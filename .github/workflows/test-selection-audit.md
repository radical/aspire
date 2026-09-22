---
description: |
  Weekly audit of Aspire PR CI's dynamic test selection (`tools/SelectTests`,
  `eng/github-ci/test-trigger-map.yml`). Looks for pull requests where the
  selector fell back to running ALL tests, classifies why, and files an issue
  for the highest-confidence cases where the selection could safely run
  fewer tests. Never edits the trigger map itself — a human (or an assigned
  Copilot coding agent session) reviews the finding and makes the change.

max-daily-ai-credits: -1

on:
  schedule: weekly on monday
  workflow_dispatch:
    inputs:
      lookback_days:
        description: "How many days of PRs/CI runs to analyze (default: 7)"
        required: false
        type: number
      pr_numbers:
        description: "Optional: comma-separated PR numbers to focus on instead of the lookback window"
        required: false
        type: string

# Only run in the canonical repository. Forks don't have the required
# secrets/permissions for this report workflow.
if: github.repository == 'microsoft/aspire'

permissions:
  contents: read
  issues: read
  pull-requests: read
  actions: read
  copilot-requests: write

concurrency:
  job-discriminator: ${{ github.event.inputs.pr_numbers || github.run_id }}

engine: copilot

network: defaults

tools:
  github:
    # Only reads: PRs, their CI runs/artifacts/summaries, and repository
    # source (selector implementation, trigger map, docs). No write access
    # is granted to the agent itself; all writes go through safe-outputs.
    # Default integrity filtering applies (public repo -> "approved") since
    # this workflow reads PR content, including from forks.
    toolsets: [repos, pull_requests, actions]
    lockdown: false

safe-outputs:
  create-issue:
    title-prefix: "[test-selection-audit] "
    labels: [area-testing, ci]
    max: 3

---

# Weekly CI test-selection audit

Audit Aspire's dynamic test selection for pull requests, find the
highest-confidence cases where the selector ran **ALL tests** unnecessarily,
and file one issue per high-confidence finding describing the fix. Do not
make any code changes yourself — a maintainer (or an assigned Copilot coding
agent session working from your filed issue) implements and validates the
fix in a follow-up PR.

## Scope and data sources

- Lookback: the last `${{ github.event.inputs.lookback_days }}` days of pull
  requests and CI runs, or **7 days** if that input is empty. If
  `${{ github.event.inputs.pr_numbers }}` is set, analyze only those PRs
  (ignore the lookback window for selecting PRs, but still use it as context
  when useful).
- Primary evidence, in order of preference:
  1. The PR's test-selection comment (posted by CI on same-repo PRs).
  2. When no comment exists (fork PRs don't get commented on) — the latest
     relevant CI workflow run for that PR/commit: read its job summary, or
     download the `select-tests-selection-Linux` artifact and read the
     selection result from it. Treat this as the authoritative source for
     fork PRs; do not report a fork PR as "no data" just because there is no
     PR comment.
- If a PR has multiple CI attempts, use the most recent completed attempt.

## Audit procedure

1. **Enumerate.** List recent PRs (or the given PR numbers) and their
   selection results. For each, capture: PR number, run ID, attempt,
   timestamp, changed files, selected project/test count, whether the
   selection was `ALL`, the escalation reason, and any unmatched/unattributed
   file that caused the escalation.
2. **Classify.** Group `ALL` selections by the triggering file/path/rule.
   Quantify frequency (how many PRs/runs hit each trigger) and keep 2-3
   concrete example PRs per trigger.
3. **Prefer safety over CI savings.** Do not propose narrowing a selection
   unless the file's real consumers are known and either existing tests
   already cover the invariant, or a focused guard test could be added that
   would fail if the narrowed behavior regressed. A missed test is worse
   than an extra CI run — when in doubt, do not propose narrowing.
4. **Do not question broad build-input files.** Files like
   `Directory.Packages.props`, `Directory.Build.props`,
   `Directory.Build.targets`, `NuGet.config`, `eng/Versions.props`, and
   `src/Directory.Build.props` legitimately affect nearly the entire .NET
   project graph — treat their `ALL` escalation as correct-by-design and do
   not flag it as a finding, even if it looks broad.
5. **Verify against source, not memory.** For every candidate, read the
   actual selector implementation, `eng/github-ci/test-trigger-map.yml`,
   `docs/ci/test-trigger-map.md`, and the real changed-file list from the
   example PRs before concluding the selection is wrong. Do not speculate
   about what a file "probably" affects.
6. **Only report high-confidence findings.** Acceptable findings include: a
   path rule that is broader than its actual consumers, a missing path rule
   that would let a runtime-only consumer (e.g. a test fixture, generated
   AppHost, or package copied into an E2E workspace) silently rely on the
   ALL fallback, an orphaned/renamed input that only ever hits the fallback,
   or a rule that looks unnecessary entirely (e.g. a file whose change
   cannot affect any test outcome — treat `.gitattributes`-style metadata
   files as an example of "runs everything for no functional reason" only if
   you have verified nothing in the trigger map or CI depends on it).
7. **No finding, no issue.** If nothing rises to high confidence, do not
   file anything. It is fine for a run to produce zero issues.

## What each filed issue must contain

For each high-confidence finding, file **one** issue with:

- **Symptom**: the concrete over-selection — which PR(s)/run(s), what
  changed, and that the selector ran ALL tests as a result. Quote the
  selection reason/log line verbatim where possible.
- **Evidence**: links to the PR(s), CI run(s)/artifact(s), and the specific
  trigger-map rule or code path responsible.
- **Root cause**: why the current rule is broader (or narrower/missing)
  than necessary, in one or two plain-language sentences.
- **Suggested fix**: a specific, scoped change to
  `eng/github-ci/test-trigger-map.yml` (or the selector) — not a rewrite of
  the selection design. Note any test that should be added under
  `tests/Infrastructure.Tests/TestTriggerMap/` to pin the new behavior.
  Mention that `docs/ci/test-trigger-map.md` may need a matching update.
- **Confidence caveat**: state explicitly that this issue was generated by
  an automated audit and the suggested fix has **not** been validated by
  running the selector or tests — whoever implements it must verify with
  `tools/SelectTests` (before/after selected-project counts) and the
  `Infrastructure.Tests/TestTriggerMap` suite before opening a PR.
- Footer: `<sub>Automated by the weekly CI test-selection audit workflow.</sub>`

## Run summary (always report, regardless of whether issues were filed)

In your final response, report:

- How many PRs/runs were analyzed and over what window (or which PR numbers,
  if explicitly given).
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts.
- Candidates you considered but rejected as correct-by-design or
  insufficiently confident, and why.
- The issues filed this run (if any), or a one-line note that no
  high-confidence finding was found this week.
