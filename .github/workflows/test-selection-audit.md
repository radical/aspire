---
description: |
  Weekly audit of Aspire PR CI's dynamic test selection (`tools/SelectTests`,
  `eng/github-ci/test-trigger-map.yml`). Looks for pull requests where the
  selector fell back to running ALL tests, classifies why, checks how
  similar cases were handled in the trigger map's own commit history, and
  files at most one issue per run for its single highest-confidence case
  where the selection could safely run fewer tests. Per-PR results and
  per-rule verdicts persist across runs in a memory branch, so escalation
  counts accumulate into cross-run evidence and no PR is analyzed twice.
  The filed issue is assigned to the Copilot coding agent, which
  implements and validates the fix and opens a PR for human review. This
  workflow never edits the trigger map itself.

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

  # A completed CI run's selection result never changes, so re-deriving it
  # every week is wasted budget -- and with a 14-day window on a weekly
  # schedule, consecutive runs overlap by about half. `processed-runs.jsonl`
  # records the PR head commits already resolved so they are never looked at
  # twice. It keys on pr+sha rather than the PR alone because an open PR
  # keeps gaining commits, each with its own selection. It is pruned to the
  # lookback window, since older commits are never enumerated again.
  #
  # `watchlist.jsonl` is the durable half. A rule that escalates to ALL a
  # few times in one window is weak evidence, but the same rule accumulating
  # escalations week after week is worth acting on -- and that is only
  # visible if the counts survive across runs.
  #
  # Repo memory, not cache memory: GitHub Actions evicts unused caches after
  # 7 days, which is exactly this workflow's period, so a cache would
  # routinely be gone by the next run. Repo memory is branch-backed and
  # retained indefinitely.
  repo-memory:
    branch-name: memory/test-selection-audit
    description: "Resolved PR selections and the rule watchlist for the CI test-selection audit"
    # Both ledgers are JSONL because gh-aw union-merges .jsonl on conflict,
    # so an append from one run can never clobber another's rows.
    file-glob: ["*.jsonl", "*.md"]
    allowed-extensions: [".jsonl", ".md"]
    # Defaults (100KB file / 10KB patch) are too small: a run appends a row
    # per resolved CI run, and a busy window covers a few hundred.
    max-file-size: 2097152
    max-patch-size: 262144
    max-file-count: 10

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

1. **Load what previous runs already know.** Persistent memory for this
   workflow is mounted at `/tmp/gh-aw/repo-memory/default/`. Read these two
   files if they exist (on the very first run they will not — that is
   normal, treat both as empty and carry on):

   - `processed-runs.jsonl` — the "already looked at, nothing to do here"
     ledger. One terse row per PR head commit whose selection you have
     already resolved:
     `{"pr": 20131, "sha": "a1b2c3d", "all": false, "seen": "2026-09-22"}`.

     **`sha` is required on every row.** Key on the commit, never on the
     PR alone. Selection is a function of the files changed at a given
     commit, so a `pr`+`sha` pair is settled permanently — but a *pull
     request* is not: an open PR keeps gaining commits, each with its own
     selection. A row carrying only `pr` would make the next run skip that
     PR forever and silently miss every push after the one you saw.

     Use the head SHA that pass 1 already returns, abbreviated to 7
     characters. Do not spend extra calls establishing identity; if you
     genuinely cannot determine the head SHA for a PR, omit the row
     entirely rather than writing one without `sha`.

     Keep rows minimal. This file is read back in full on every run, so
     anything beyond identity and outcome costs context forever and buys
     nothing — the interesting detail belongs in `watchlist.jsonl`. The one
     useful extra is `"rule"` on an `all: true` row, which lets a later run
     recount escalations without re-fetching.

   - `watchlist.jsonl` — the rules worth continuing to watch. One row per
     trigger you have judged:
     `{"rule": ".github/actions/**", "verdict": "watch", "all_runs": 12, "first_seen": "2026-09-08", "last_seen": "2026-09-22", "example_prs": [20131, 20046], "note": "...", "ref": null}`.

     `verdict` is one of:

     - `watch` — a plausible candidate that has not yet cleared the
       confidence bar. Keep accumulating evidence for it.
     - `correct-by-design` — settled; stop re-deriving it.
     - `filed` — **this workflow** filed an issue for it. Put the issue
       number in `ref`.
     - `in-flight` — someone else is already fixing it (see step 8). Put
       the PR number in `ref`. Do not record this as `filed`: the two
       decay differently, since an in-flight PR can be closed unmerged and
       the rule then returns to `watch`, whereas a filed issue stays ours.

     Use `correct-by-design` for anything the prompt tells you to reject as
     intended behavior rather than as weak evidence — a file on the
     build-input list in step 5, or a self-referential selector change in
     step 6. Those are settled, not still being watched.

     Only record a rule you actually observed escalating this window, or
     one already carried forward from a previous run. Do not seed a row
     for a rule you merely noticed sharing a fix with an observed one — a
     row with `all_runs: 0` is noise that dilutes the counts this ledger
     exists to accumulate.

     Carry these forward rather than re-deriving them. For a rule recorded
     `correct-by-design`, do not re-read the trigger map and its history
     again unless the rule's own text has changed since `last_seen`; just
     add this window's counts and move on.

   **Rows written by an older version of this prompt may not match the
   shapes above.** Never delete or rewrite a row just because its shape is
   unfamiliar, and never invent a missing field to make one conform. Treat
   an unrecognized field as extra detail and ignore it; treat a missing
   field as unknown. In particular, a `processed-runs.jsonl` row with no
   `sha` cannot prove that any specific commit was resolved, so it must
   not cause a PR to be skipped — re-analyze that PR and append a proper
   `pr`+`sha` row alongside. Leave the old row in place; pruning clears it
   in time.

   The watchlist is the point of this memory. A rule that escalates to ALL
   a few times in one window is weak evidence and will not clear the
   confidence bar — but the same rule accumulating escalations week after
   week is exactly the signal worth acting on, and it is only visible if
   the counts survive across runs.
2. **Enumerate, cheaply first.** Work in two passes so you do not spend the
   run's budget on PRs that selected normally.

   - *Pass 1 (broad, cheap).* Get the candidate PR list for the window in as
     few calls as possible — use `list_pull_requests`/`search_pull_requests`
     and reuse the metadata they already return — including each PR's
     **head SHA**, which you need both to skip already-resolved work and
     to write the ledger later. Skip any PR whose current head SHA already
     has a row in `processed-runs.jsonl`; a PR whose head has not moved
     since you resolved it needs no calls at all. Then read only the
     **selection comment** for each remaining PR to decide whether it
     selected `ALL`. Do not fetch per-PR metadata or changed files in this
     pass; a PR that selected normally needs no further calls.

     Identify that comment by its marker, not by prose. The selector's
     comment always begins with `<!-- select-tests-comment -->` and ends
     with a footer naming the commit it was computed for:

     ```
     <!-- select-tests-comment -->
     ...selection summary...

     ---
     _Selection computed for commit [`a1b2c3d`](.../commit/a1b2c3d...)._
     ```

     Match the marker, never the wording — a busy PR accumulates review
     chatter that mentions "all tests" for unrelated reasons. The selector
     posts **one comment per pushed commit** and updates it in place on
     re-runs, so a PR can carry several marked comments; take the most
     recent, since earlier ones describe superseded commits.

     That footer SHA is the selection's own idempotency key, so prefer it
     over the listing's head SHA when writing the ledger — it is the
     commit the result actually belongs to. If the two disagree, a newer
     commit was pushed before its selection was posted: treat that as not
     yet resolved and skip the PR this run.
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

   A PR with no marked comment is ambiguous on its own — it could be a
   same-repo PR whose CI has not finished yet, or a fork PR that will
   never get one regardless of CI state. Resolve that with one cheap call
   before deciding which bucket it falls in:

   - **Same-repo PR** (head and base share the same owner): no comment
     means the selection job has not posted yet. Check its latest run's
     status once. If it is queued/in-progress or `action_required`, skip
     it per the list above. If a run **completed** with no comment, that
     is a real gap — read its job summary/artifact instead of silently
     skipping, since something is wrong either with the selector or with
     this assumption.
   - **Fork PR** (head repo differs from base repo): no comment is
     expected regardless of CI state, by design. Check its latest run's
     status once to decide the bucket: queued/in-progress/`action_required`
     still means skip; a **completed** run means fall back to the
     selection job's log or artifact rather than skipping — do not report
     a fork PR as "no data" just because there is no PR comment.
3. **Classify.** Group `ALL` selections by the triggering file/path/rule.
   Quantify frequency (how many PRs/runs hit each trigger) and keep 2-3
   concrete example PRs per trigger.

   Then add to the counts already carried in `watchlist.jsonl` and report
   the cumulative figure alongside this window's — a rule's cross-run
   total is the strongest frequency evidence you have.

   **Count only selections you resolved for the first time this run** —
   those whose `pr`+`sha` was not already in `processed-runs.jsonl`. The
   14-day window on a weekly cadence means consecutive runs overlap by
   about half, so adding the whole window every time would silently
   double-count every carried-over commit and inflate exactly the
   evidence the watchlist exists to make trustworthy.
4. **Prefer safety over CI savings.** Do not propose narrowing a selection
   unless the file's real consumers are known and either existing tests
   already cover the invariant, or a focused guard test could be added that
   would fail if the narrowed behavior regressed. A missed test is worse
   than an extra CI run — when in doubt, do not propose narrowing.
5. **Do not question broad build-input files.** These files legitimately
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
6. **CI YAML and composite actions are in scope.** Changes under
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
7. **Verify against source, not memory.** For every candidate, read the
   actual selector implementation, `eng/github-ci/test-trigger-map.yml`,
   `docs/ci/test-trigger-map.md`, and the real changed-file list from the
   example PRs before concluding the selection is wrong. Do not speculate
   about what a file "probably" affects.
8. **Check whether a fix is already in flight.** Before going further with a
   candidate, check whether someone is already fixing it:

   - Use `search_pull_requests` for **open** PRs touching
     `eng/github-ci/test-trigger-map.yml` or naming the rule.
   - Check `watchlist.jsonl` for an `in-flight` or `filed` verdict
     recorded against this rule by an earlier run, and confirm its `ref`
     is still open — an in-flight PR that was closed unmerged no longer
     blocks the candidate.

   If a fix is already in flight, reject the candidate and say so in the
   run summary. Filing anyway would start a second coding agent on work
   that is already done and put a duplicate PR in front of a reviewer.
9. **Check how this was handled before.** Maintainers have already made
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
10. **Apply the confidence bar.** Candidate findings include: a path rule
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
11. **Pick one, or none.** If several candidates clear the bar, file only the
   strongest — the one with the clearest evidence and the most `ALL` runs
   avoided. If none clear it, file nothing. A run that files no issue is a
   normal, successful run; filing a weak finding is worse than filing
   nothing, because it starts a coding agent session and consumes human
   review time.
12. **Write back what you learned.** Before finishing, update the two
    ledgers in `/tmp/gh-aw/repo-memory/default/`. They are committed
    automatically after the run; you only need to write the files.

    - Append one row to `processed-runs.jsonl` for every PR head you
      resolved this run, including the ones that selected normally —
      recording a non-`ALL` outcome is what stops the next run from
      fetching it again. **Every row must carry both `pr` and `sha`**; a
      row without `sha` would make future runs skip that PR permanently.
      Do **not** append rows for PRs you skipped as pending /
      `action_required`: nothing was resolved, and they still need
      analysis once their CI finishes.

      Then **prune** it: drop rows whose `seen` date is older than twice
      the lookback window. Commits outside the window are never enumerated
      again, so keeping them only grows a file you re-read every time.
    - Update `watchlist.jsonl` for each rule you actually observed
      escalating this run, plus any carried forward from earlier runs:
      carry the cumulative `all_runs` count forward, refresh `last_seen`,
      and set `verdict` / `ref` to match where the rule now stands. Keep a
      rule here even when it fails the confidence bar — a `watch` row that
      keeps accumulating escalations is the evidence a future run needs to
      justify acting. Drop a rule only once it is settled
      `correct-by-design` or its fix has merged.

    Prefer appending over rewriting: both files are `.jsonl` and are
    union-merged on conflict, so an append is safe even if another run
    writes concurrently, while a rewrite can silently drop rows. When you
    must rewrite — pruning, or updating a watchlist row in place — re-read
    the file first and preserve every row you are not deliberately
    changing. Keep rows one-line and minimal; these files are size-capped
    and are read back in full on every future run.

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
  if explicitly given), and how many CI runs were skipped as already
  recorded in `processed-runs.jsonl` rather than re-fetched.
- How many PRs were skipped because they could not have a selection result
  yet (CI pending, `action_required`, or no selection job), so a quiet
  window is distinguishable from an unanalyzable one.
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts — both for this window and cumulatively across runs.
- The current watchlist: each rule being tracked, its cumulative `ALL`
  count, and how that count moved this run. A rule whose count is climbing
  week over week is the audit's main product even when nothing is filed.
- Candidates you considered but rejected as correct-by-design or as failing
  the confidence bar, and which specific criterion each one failed. Call out
  separately any candidate rejected because history shows the same change
  was already tried and reverted, and any rejected because a fix is already
  in flight.
- The issue filed this run, if any, or a one-line note that no finding
  cleared the bar this week.
