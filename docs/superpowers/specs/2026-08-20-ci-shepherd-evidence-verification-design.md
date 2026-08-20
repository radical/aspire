# CI Shepherd Evidence Verification Design

## Purpose

The CI shepherd should decide whether a reported failure is recurring, recovered,
actionable, or still waiting for evidence. It should not diagnose why a test or
product failure occurs. Root-cause investigation belongs in a separate session
with the issue's bounded evidence as its starting handoff.

The current thin POC can classify an issue from its title and compact first-pass
evidence before it has fetched referenced fixes, subsequent workflow results, or
recent occurrences. This can lock in a confident but stale judgment. For example,
a resolved compile failure can be escalated for human ownership even though its
fix merged and the affected `main` CI path later passed.

## Goals

- Preserve recent failure history even after individual issues are closed.
- Identify repeated test, network, infrastructure, workflow, and build failures.
- Verify recovery using executions that cover the same failing target.
- Increase confidence before proposing quarantine, retry, closure, or escalation.
- Produce a bounded handoff for a separate investigation session when diagnosis
  is needed.
- Keep collection read-only and recommendations review-only.

## Non-goals

- Diagnose the root cause of a failing test or product defect.
- Inspect arbitrary source code or logs beyond extracting bounded failure facts.
- Automatically close issues, quarantine tests, rerun jobs, or modify workflows.
- Treat age or generic green builds as proof that a specific failure recovered.
- Retain complete workflow logs indefinitely.

## Architecture

The shepherd uses two assessment stages separated by deterministic evidence
expansion:

1. **Baseline collection** gathers the open issue inventory and bounded factual
   evidence.
2. **Preliminary triage** identifies a failure target, a provisional queue, and
   the exact confidence gaps that could change that queue.
3. **Evidence planning** emits only allowlisted requests for referenced issues,
   pull requests, workflow runs, exact canonical searches, or source checks.
4. **Deterministic expansion** executes those GET-only requests and writes a new
   immutable snapshot.
5. **Fresh verification** invokes the existing assessment contract again for
   issues whose evidence changed. Its input is regenerated from the expanded
   snapshot and excludes preliminary judgments, avoiding anchoring without
   introducing a second prompt or judgment schema.
6. **Deterministic gates** reject unsupported quarantine, retry, closure, and
   escalation proposals before rendering the report.

The verifier may classify and summarize evidence. It must not perform an
open-ended investigation. When the evidence establishes that diagnosis is
needed, it emits an investigation handoff rather than attempting the diagnosis.

## POC Failure History

The first POC writes one append-only `fingerprints.jsonl` independent of issue
state. Each run adds normalized occurrences and readers deduplicate them by
fingerprint, run, attempt, and issue:

- Do not retain logs or full issue bodies.
- Do not add rollups, retention garbage collection, or a second state database.
- Query recent windows from occurrence timestamps.
- Keep the file between live trials so closed issue records still contribute to
  recurrence.

An occurrence records:

- Stable failure fingerprint and normalized cause family.
- Exact test name when available.
- Workflow, job, step, platform, branch, commit, run, and attempt.
- Failure date and outcome.
- Whether the record is a rerun of the same execution or an independent run.
- The issue or tracker that reported it.

The fingerprint uses the most specific stable identity available: exact test,
error code or exception, then compatible workflow/job/step and normalized
message shape. Separate attempts of one run do not count as independent
recurrence. Closed issues remain represented through their occurrence records,
so repeated network or infrastructure failures can be detected without reopening
or reusing stale issue records.

Thirty-day detail retention and 90-day aggregate buckets are deferred until live
trials establish that the ledger is useful and reveal the appropriate windows.

## Verification Proof Packs

### Flaky-test candidate

A quarantine proposal requires:

- An exact test identity.
- At least two independent failures on at least two distinct days.
- Compatible failure signatures.
- A completed exact canonical issue and quarantine-state lookup.

A deterministic prerequisite or dependency failure remains an investigation
candidate even when it affects a test. If recurrence is below threshold, the
issue stays on watch with the precise event that would end the watch.

For the POC, a later successful run of the same workflow and branch is supporting
recovery evidence, not proof that the exact test passed. Quarantine remains a
review-only proposal with medium confidence. Test-result-level pass evidence is
deferred until the trials show it is necessary and available.

### Network or transient infrastructure candidate

The shepherd groups stable network fingerprints such as endpoint category,
status or socket code, workflow/job/step, and platform without retaining
sensitive endpoint values.

- One occurrence followed by covered successful executions is a one-off incident
  and may be proposed for closure while remaining in history.
- Recurrence across at least three independent runs and two days is eligible for
  retry-policy review.
- A repeated deterministic response such as HTTP 404 is an investigation, not a
  transient retry candidate.
- A rerun that passes is useful recovery evidence but is not an independent
  failure.

### Build, compilation, packaging, or configuration failure

The verifier checks referenced fixes and subsequent executions of the same
relevant workflow and job:

- A merged fix plus a later covered success and no newer matching failure yields
  a closure proposal.
- Without an explicit fix, later covered successes can still establish a
  one-off recovered incident when the same failing target was exercised.
- A generic green branch run is insufficient when the failing job was skipped or
  the affected target was not built.
- A current or recurring deterministic failure yields an investigation handoff.
- Human escalation is allowed only for an explicit permission, access, policy,
  or prioritization decision that automation cannot make.

### Watch

Watch is used only after current evidence has been exhausted. It records:

- Why the current threshold is not met.
- The exact future occurrence, successful execution, or time boundary that ends
  the watch.
- The resulting queue for each possible outcome.

A single suspected flaky failure that does not recur should not eventually ping
a human merely because time passed. A configurable recent-history window plus
covered successful executions can support closure as a one-off. Missing coverage
remains an evidence gap.

## Investigation Handoff

When diagnosis is necessary, the final JSON includes:

- Source issue and canonical failure target.
- Normalized failure fingerprint.
- Recent independent occurrences and relevant successful executions.
- Evidence already collected.
- The specific unanswered diagnostic question.
- Suggested specialist workflow, such as flaky-test, CI failure, deployment, or
  workflow investigation.

The shepherd does not launch that investigation in the initial POC. The handoff
uses enumerated fields for issue, target, fingerprint, occurrences, unanswered
question, and routing hint. It has no free-form root-cause analysis field. A
separate executor can later create an independent session from this handoff.

## Decision Output

The final report joins these fields deterministically:

- `provisionalDisposition`
- `verifiedDisposition`
- `confidence`
- `proofGate`
- `evidenceIds`
- `historyWindow`
- `coverage`
- `remainingGaps`
- optional `investigationHandoff`

The verifier sees only the expanded issue evidence and never sees
`provisionalDisposition`. It must cite the evidence satisfying every proof gate.
If an expansion fails or is truncated, the corresponding gate remains
unsatisfied.

## First Trial Cut

The first trial intentionally supports:

- One adaptive expansion round.
- `issue-reference` and `workflow-run` requests only.
- No `source-check` or canonical-search expansion.
- The same assessment prompt and judgment schema before and after expansion.
- Fresh assessment only for issues named by the expansion manifest.
- Two enforced action gates:
  - quarantine requires two independent failures on two days with compatible
    signatures;
  - closure requires a merged fix plus later covered success, or later covered
    success with no newer matching failure.

Retry policy tuning, 90-day aggregation, test-result-level pass evidence, and
automatic investigation-session launching are deferred.

## Error Handling

- Expansion requests remain allowlisted and issue-scoped. The first trial is
  bounded to one round and 25 requests.
- Partial or failed reads are explicit and cannot be treated as negative search
  results or successful coverage.
- History deduplication rejects duplicate run attempts and duplicate issue rows.
- Unsupported or ambiguous fingerprints remain separate rather than inflating
  recurrence counts.
- The previous successful history snapshot remains available if a new collection
  cannot complete.

## Validation

End-to-end fixtures should prove:

- A resolved compile failure such as issue #19149 becomes a closure proposal
  after its fixing pull request and covered successful `main` run are enriched.
- A recurring test is not proposed for quarantine without successful executions
  or compatible recurrence evidence; its POC recommendation remains review-only.
- A consistently failing test becomes an investigation handoff, not a flaky-test
  quarantine proposal.
- Repeated network fingerprints survive closure of individual incident issues
  and reach the retry-review threshold across the rolling ledger.
- A one-off build failure followed by covered green executions becomes a closure
  proposal.
- A green run that skipped the failing job does not prove recovery.
- Generic ownership questions cannot produce human escalation.
- The expanded assessment input contains no preliminary disposition text.
- At least one disposition changes because of a cited expanded evidence record;
  otherwise the second pass does not justify its cost.
