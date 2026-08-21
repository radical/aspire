# CI Shepherd Thin POC Design

## Purpose

The next milestone is not a production-safe executor. It is a fast,
repeatable experiment that shows whether a fresh agent can turn existing CI
issue evidence into useful review queues.

The POC should make scripts and prompts cheap to change, then run them against
live issues. Production-grade lifecycle rules, systemic rate calculations,
executor capabilities, and mutation safety remain deferred until the output
demonstrates which decisions are valuable.

## Flow

1. Reuse the existing incremental collector and private history.
2. Project each open issue into a compact evidence bundle.
3. Give those bundles to one fresh assessment agent in batches.
4. Let the agent emit evidence-linked typed judgments.
5. Validate only structure, vocabulary, issue coverage, and evidence
   references.
6. Render deterministic Markdown queues.
7. Save the JSON and report for comparison with later trials.

## POC Output

Each open issue receives:

- A broad failure category:
  - `flaky-test`
  - `transient-infrastructure`
  - `blocking-build`
  - `product-or-tooling`
  - `automation-tracker`
  - `unknown`
- A recommended disposition:
  - `investigate`
  - `watch`
  - `ping-human`
  - `review-quarantine`
  - `review-retry`
  - `review-rerun`
  - `review-close`
  - `no-action`
- Confidence, summary, evidence IDs, missing evidence, and the condition that
  should trigger reassessment.

The POC may emit several recommendations for one issue when their targets
differ, such as investigating the issue while reviewing a specific test for
quarantine.

## Deterministic Guardrails

- The agent may cite only evidence from its bounded issue bundle.
- Every open issue must have at least one judgment.
- Unknown categories and conservative dispositions are valid.
- A later attempt of the same run is labeled as a rerun, not independent
  recovery.
- Missing positive execution evidence must be visible in `missingEvidence`.
- No GitHub write is performed.

These guardrails make bad output visible without encoding every future policy
decision before the first useful trial.

## Deferred Work

- Automatic comments, labels, closure, reruns, assignments, and PRs.
- Executor capability and approval models.
- Precise quarantine-entry thresholds.
- Complete lifecycle state machines.
- Cross-issue systemic failure rates.
- Production-stable occurrence identifiers.
- Strict recomputation and stale-proposal rejection.

## Success Criteria

- A warm run reaches the agent quickly using existing history reuse.
- The agent produces valid JSON for every open issue.
- Markdown separates investigation, watching, human, quarantine, retry, rerun,
  and closure-review queues.
- A human can identify useful, incorrect, and missing recommendations from one
  report and adjust the prompt or projection for the next run.
- The POC remains read-only.
