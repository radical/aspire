---
description: |
  Weekly audit of Aspire PR CI's dynamic test selection (`tools/SelectTests`,
  `eng/github-ci/test-trigger-map.yml`). Looks for pull requests where the
  selector fell back to running ALL tests, classifies why, and files at most
  one issue per run for its single highest-confidence case where the
  selection could safely run fewer tests. The filed issue is assigned to the
  Copilot coding agent, which implements and validates the fix and opens a
  PR for human review. This workflow never edits the trigger map itself.

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
    # Assigning `copilot` starts a Copilot coding agent session on the filed
    # issue, which implements and validates the fix and opens a PR for human
    # review. This requires the `GH_AW_AGENT_TOKEN` fine-grained PAT secret;
    # without it the issue is still filed but assignment fails.
    assignees: [copilot]
    # One finding per run. Each filed issue starts a coding agent session and
    # ends in a PR a human must review, so the workflow surfaces only its
    # single highest-confidence finding rather than a batch of candidates.
    max: 1
    # A weekly schedule would otherwise re-file the same finding (and start a
    # duplicate agent session) every run. Titles name the offending rule, so
    # near-exact matches against open and recently-closed issues are dropped.
    deduplicate-by-title: 1

---

# Weekly CI test-selection audit

Audit Aspire's dynamic test selection for pull requests and find the
**single highest-confidence** case where the selector ran **ALL tests**
unnecessarily. If you find one, file an issue describing the fix.

The issue you file is automatically assigned to the Copilot coding agent,
which will implement and validate the fix and open a pull request for human
review. So the issue is not a report — it is a **task specification** for
another agent, and filing one commits real review effort. Do not make any
code changes yourself.

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
6. **Apply the confidence bar.** Candidate findings include: a path rule
   broader than its actual consumers, a missing path rule that would let a
   runtime-only consumer (e.g. a test fixture, generated AppHost, or package
   copied into an E2E workspace) silently rely on the ALL fallback, an
   orphaned/renamed input that only ever hits the fallback, or a rule that
   looks unnecessary entirely (e.g. a file whose change cannot affect any
   test outcome — treat `.gitattributes`-style metadata files as an example
   of "runs everything for no functional reason" only if you have verified
   nothing in the trigger map or CI depends on it).

   File a candidate **only if all of these hold**:
   - You identified the exact rule or code path responsible, by reading it.
   - You enumerated the file's real consumers from repository source, not
     from what the name suggests.
   - You can name the specific narrowed rule the fix should produce.
   - You can name a test that would fail if the narrowing were wrong.
   - You would be comfortable defending the change in review.

   If any of those is missing, it is not high-confidence. Report it in the
   run summary instead.
7. **Pick one, or none.** If several candidates clear the bar, file only the
   strongest — the one with the clearest evidence and the most `ALL` runs
   avoided. If none clear it, file nothing. A run that files no issue is a
   normal, successful run; filing a weak finding is worse than filing
   nothing, because it starts a coding agent session and consumes human
   review time.

## The issue you file

Write the issue as a task specification for the Copilot coding agent that
will be assigned to it — it should be able to start work from the issue
alone, without re-doing your analysis.

Title it so it names the offending rule or input, for example
`Narrow <rule/path> so it no longer escalates test selection to ALL`.
Titles are deduplicated against open and recently-closed issues, so a stable,
specific title prevents re-filing the same finding on a later run.

The body must contain:

- **Symptom**: the concrete over-selection — which PR(s)/run(s), what
  changed, and that the selector ran ALL tests as a result. Quote the
  selection reason/log line verbatim in a fenced code block.
- **Evidence**: links to the PR(s), CI run(s)/artifact(s), and the specific
  trigger-map rule or code path responsible, with file and line.
- **Root cause**: why the current rule is broader (or narrower/missing)
  than necessary, in one or two plain-language sentences.
- **Suggested fix**: the specific, scoped change to
  `eng/github-ci/test-trigger-map.yml` (or the selector) — not a rewrite of
  the selection design. Name the test to add or update under
  `tests/Infrastructure.Tests/TestTriggerMap/` to pin the new behavior, and
  note whether `docs/ci/test-trigger-map.md` needs a matching update.
- **Required validation** (the assigned agent must do this before opening a
  PR, and must not claim success without it):
  - Run `tools/SelectTests` against the changed-file lists from the example
    PRs, and report selected-project counts before and after the change.
  - Run the guard tests:
    `dotnet test --project tests/Infrastructure.Tests/Infrastructure.Tests.csproj --no-launch-profile -- --filter-namespace "*.TestTriggerMap" --filter-not-trait "quarantined=true" --filter-not-trait "outerloop=true"`
  - Open the result as a **draft** PR that links back to this issue.
- **Unvalidated-analysis caveat**: state that this issue came from an
  automated audit and the suggested fix has not been validated by running
  the selector or tests. If validation contradicts the analysis here, the
  assigned agent should say so on the issue and not force the change
  through.
- Footer: `<sub>Automated by the weekly CI test-selection audit workflow.</sub>`

## Run summary (always report, regardless of whether an issue was filed)

In your final response, report:

- How many PRs/runs were analyzed and over what window (or which PR numbers,
  if explicitly given).
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts.
- Candidates you considered but rejected as correct-by-design or as failing
  the confidence bar, and which specific criterion each one failed.
- The issue filed this run, if any, or a one-line note that no finding
  cleared the bar this week.
