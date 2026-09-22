---
description: |
  Weekly audit of Aspire PR CI's dynamic test selection (`tools/SelectTests`,
  `eng/github-ci/test-trigger-map.yml`). Looks for pull requests where the
  selector fell back to running ALL tests, classifies why, checks how
  similar cases were handled in the trigger map's own commit history, and
  files at most one issue per run for its single highest-confidence case
  where the selection could safely run fewer tests. The filed issue is
  assigned to the Copilot coding agent, which implements and validates the
  fix and opens a PR for human review. This workflow never edits the
  trigger map itself.

max-daily-ai-credits: -1

on:
  schedule: weekly on monday
  workflow_dispatch:
    inputs:
      lookback_days:
        description: "How many days of PRs/CI runs to analyze (default: 14)"
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
  requests and CI runs, or **14 days** if that input is empty. The window is
  deliberately wider than the weekly cadence: a single week's merges are
  mostly routine and tend to surface only correct-by-design escalations, so
  a one-week window produces empty runs. Overlapping windows are safe
  because findings are deduplicated by title. If
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

1. **Enumerate, cheaply first.** Work in two passes so you do not spend the
   run's budget on PRs that selected normally.

   - *Pass 1 (broad, cheap).* Get the candidate PR list for the window in as
     few calls as possible — use `list_pull_requests`/`search_pull_requests`
     and reuse the metadata they already return. Then read only the
     **selection comment** for each PR to decide whether it selected `ALL`.
     Do not fetch per-PR metadata or changed files in this pass; a PR that
     selected normally needs no further calls.
   - *Pass 2 (narrow, detailed).* Only for PRs that selected `ALL`, capture:
     PR number, run ID, attempt, timestamp, changed files, selected
     project/test count, the escalation reason, and any
     unmatched/unattributed file that caused the escalation.

   **Skip, without spending further calls on them**, any PR that cannot have
   a selection result yet:

   - CI still pending — runs that are queued or in progress. The selection
     may not be posted yet, or may still change on a later attempt.
   - CI in `action_required` — a fork PR waiting on maintainer approval
     before workflows run. No selection has happened at all.
   - No CI run in the window, or the selection job did not run.

   These are not findings and they are not "no comment" cases; do not fall
   back to reading their runs. Count them and report the total as skipped in
   the run summary, so a window that looks quiet for this reason is
   distinguishable from one that genuinely had no `ALL` selections.

   When a PR has no selection comment but its CI **did** complete (fork PRs
   don't get commented on), fall back to its CI run — the selection job's
   log or artifact — rather than skipping it.
2. **Classify.** Group `ALL` selections by the triggering file/path/rule.
   Quantify frequency (how many PRs/runs hit each trigger) and keep 2-3
   concrete example PRs per trigger.
3. **Prefer safety over CI savings.** Do not propose narrowing a selection
   unless the file's real consumers are known and either existing tests
   already cover the invariant, or a focused guard test could be added that
   would fail if the narrowed behavior regressed. A missed test is worse
   than an extra CI run — when in doubt, do not propose narrowing.
4. **Do not question broad build-input files.** These files legitimately
   affect nearly the entire .NET project graph — treat their `ALL`
   escalation as correct-by-design and do not flag it as a finding, even if
   it looks broad:

   `Directory.Packages.props`, `Directory.Build.props`,
   `Directory.Build.targets`, `NuGet.config`, `eng/Versions.props`,
   `eng/Version.Details.xml`, `src/Directory.Build.props`, `global.json`,
   and `Aspire.slnx`.

   **That list is exhaustive.** It is not a category to reason by analogy
   from. A file is exempt because it appears above, not because it sits at
   the repository root, has a `.props`/`.json` extension, or looks
   infrastructural. In particular, `.gitattributes`, `.editorconfig`,
   `.config/dotnet-tools.json`, and CI YAML are **not** on this list and
   must be judged on their actual effect like anything else — a previous
   run wrongly waved `.gitattributes` through as a "broad build input",
   which is exactly the kind of over-selection this audit exists to catch.
5. **CI YAML and composite actions are in scope.** Changes under
   `.github/workflows/**` and `.github/actions/**` are a frequent `ALL`
   trigger, and unlike build inputs they are *not* automatically
   correct-by-design. A workflow or action that gates exactly one job, or
   whose change cannot affect any test outcome at all (release gating,
   labeling, issue automation, docs publishing), is a legitimate finding —
   do not wave it through just because the rule that matched it carries a
   comment. Judge the specific file's real effect, not the rule's blurb.

   Two things to get right before proposing anything here:

   - **`.github/actions/**` -> ALL is pinned by a guard test.** The map
     routes every local composite action to ALL, and
     `TestTriggerMapTests.EveryLocalActionUsedByAWorkflowIsRoutedToAll`
     asserts that every action referenced by any workflow stays routed that
     way. So a narrowing here is not a one-line map edit: your suggested fix
     must say explicitly how that test's contract changes (for example, a
     documented exception list the test honors) and must treat updating the
     test as part of the work. If you cannot describe that coherently, the
     candidate fails the confidence bar.
   - **A self-referential ALL is correct.** If the PR modified the selector
     itself — `tools/SelectTests`, `eng/github-ci/test-trigger-map.yml`, or
     the select-tests action/workflow — then running ALL is the intended
     safety behavior, not an over-selection bug. Reject those.
6. **Verify against source, not memory.** For every candidate, read the
   actual selector implementation, `eng/github-ci/test-trigger-map.yml`,
   `docs/ci/test-trigger-map.md`, and the real changed-file list from the
   example PRs before concluding the selection is wrong. Do not speculate
   about what a file "probably" affects.
7. **Check whether a fix is already in flight.** Before going further with a
   candidate, use `search_pull_requests` to look for **open** PRs touching
   `eng/github-ci/test-trigger-map.yml` or naming the rule.

   If a fix is already in flight, reject the candidate and say so in the
   run summary. Filing anyway would start a second coding agent on work
   that is already done and put a duplicate PR in front of a reviewer.
8. **Check how this was handled before.** Maintainers have already made
   many of these decisions, and the trigger map records them. For each
   surviving candidate — not up front, and not for the whole file — use
   `list_commits` with `path: eng/github-ci/test-trigger-map.yml` and
   `get_commit` to read the commits that last touched the rule or section
   you are about to change, plus their PR discussion. Keep `perPage` small
   (5-10) — commit messages in this repository are long, and a wide page
   costs far more context than it returns. Use this for three things:

   - **Pick an existing fix shape.** The map has distinct mechanisms —
     `prefilter`, `ignore`, `path_rules`, `affected_project_rules`,
     `derived_targets`, and `groups` — and they are not interchangeable.
     In particular, when a file genuinely cannot affect any test outcome,
     the established fix is an `ignore:` entry with a comment saying why
     (e.g. "Layer 1 covers", "no GH-CI consumer"), **not** a narrowed
     `path_rules` target. Match the surrounding comment convention,
     including its habit of documenting deliberate *non*-entries.
   - **Respect recorded failures.** If history shows a rule was already
     narrowed and later widened back (or an `ignore` entry was removed),
     that is direct evidence the narrowing was wrong. Do not propose it
     again — report it in the run summary as previously-tried instead.
   - **Look for missed siblings.** If a past commit routed one consumer of
     a shared input but left sibling consumers on the fallback, that gap
     is itself a strong candidate.
9. **Apply the confidence bar.** Candidate findings include: a path rule
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
   - You can name the specific narrowed rule the fix should produce, using
     one of the map's existing mechanisms.
   - You can name a test that would fail if the narrowing were wrong.
   - If an existing guard test currently pins the behavior you want to
     change, you can state how that test's contract should change.
   - History does not show this same narrowing already being tried and
     reverted.
   - You would be comfortable defending the change in review.

   If any of those is missing, it is not high-confidence. Report it in the
   run summary instead.
10. **Pick one, or none.** If several candidates clear the bar, file only the
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
  the selection design. Say which mechanism it uses (`prefilter`, `ignore`,
  `path_rules`, `affected_project_rules`, `derived_targets`, or `groups`)
  and cite a prior commit that used the same shape, so the assigned agent
  follows established convention rather than inventing one. Name the test to
  add or update under `tests/Infrastructure.Tests/TestTriggerMap/` to pin the
  new behavior, and note whether `docs/ci/test-trigger-map.md` needs a
  matching update.
- **Prior art**: the commits you consulted for this rule and what they
  establish. If history shows a related change that was reverted, say so
  and explain why this proposal is different.
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
- How many PRs were skipped because they could not have a selection result
  yet (CI pending, `action_required`, or no selection job), so a quiet
  window is distinguishable from an unanalyzable one.
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts.
- Candidates you considered but rejected as correct-by-design or as failing
  the confidence bar, and which specific criterion each one failed. Call out
  separately any candidate rejected because history shows the same change
  was already tried and reverted, and any rejected because a fix is already
  in flight.
- The issue filed this run, if any, or a one-line note that no finding
  cleared the bar this week.
