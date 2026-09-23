---
description: |
  Weekly audit of Aspire PR CI's dynamic test selection (`tools/SelectTests`,
  `eng/github-ci/test-trigger-map.yml`). Looks for pull requests where the
  selector fell back to running ALL tests (over-selection) or where a
  narrow rule's target list misses a real consumer (under-selection),
  classifies why, checks how similar cases were handled in the trigger
  map's own commit history, and files at most one issue per run for its
  single highest-confidence case where the selection could be made safer
  or cheaper. Per-PR results and per-rule verdicts persist across runs in
  a memory branch, so escalation counts accumulate into cross-run
  evidence without double-counting PR heads or their reruns.
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
  # gh-aw's compiler always emits a static top-level group for this
  # workflow ("gh-aw-${{ github.workflow }}", queue: max) in addition to
  # whatever this field configures, and that group has no awareness of
  # `pr_numbers` — it serializes every run of this workflow, full-window
  # or PR-focused, one at a time, queued in trigger order. That is
  # deliberate here, not just an accepted side effect: two agent runs
  # executing concurrently would each read the memory ledger from the
  # same base and independently rewrite it (head replacements and
  # watchlist updates); the push that lands second can discard the first's
  # rows, even for appends (see step 13). This job-discriminator only
  # scopes the agent job's own concurrency group; it cannot change the
  # top-level group's serialization.
  job-discriminator: ${{ github.event.inputs.pr_numbers || github.run_id }}

engine: copilot

network:
  allowed:
    - defaults
    - github-actions

tools:
  bash: ["cat", "ls", "grep", "head", "tail", "wc", "curl", "unzip"]
  github:
    # Only GitHub MCP reads: PRs, their CI runs/artifacts, repository source
    # (selector implementation, trigger map, docs), and issues (to
    # reconcile a `pending-filed` watchlist row against the real issue
    # `create-issue` produced, per step 1). The `issues` toolset also
    # exposes `create_issue`, but the GitHub MCP server always runs with
    # `GITHUB_READ_ONLY: "1"` regardless of toolset -- write tools are
    # non-functional here. All writes go through safe-outputs instead.
    # The default "approved" integrity filter would hide fork PRs from
    # first-time/external contributors -- exactly the fork PRs this audit
    # is meant to cover (see "Primary evidence" below), so it is disabled
    # here. GitHub mutations are limited to one safe-output issue
    # (create-issue, max: 1), whose assignment starts a coding agent;
    # the resulting PR still requires human review before merging.
    # Untrusted fork content can also influence shell commands: `curl`
    # has outbound access to allowlisted domains for signed CI artifacts.
    # Only fetch artifact URLs returned by GitHub's actions API, never
    # a URL or shell fragment supplied by a PR.
    toolsets: [repos, pull_requests, actions, issues]
    min-integrity: none

  # A selection is fixed for a particular CI run/attempt, not for a PR
  # head: re-runs can replace its result. `processed-runs.jsonl` retains
  # each head and its counted path contributions so a newer attempt can
  # replace, rather than add to, its earlier counts. Do not prune identities:
  # even an old head may be revisited by a focused dispatch or PR update.
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
    # Both ledgers are JSONL so individual observations can be counted and
    # updated. gh-aw's push retry uses `git pull --no-rebase -X ours` (step
    # 13), not a JSONL-aware merge; even two appends can conflict and lose
    # rows. The workflow-level concurrency group protects these ledgers.
    file-glob: ["*.jsonl", "*.md"]
    allowed-extensions: [".jsonl", ".md"]
    # Defaults (100KB file / 10KB patch) are too small: the durable index
    # retains one row per resolved PR head and a busy window covers hundreds.
    max-file-size: 2097152
    max-patch-size: 262144
    max-file-count: 10

safe-outputs:
  create-issue:
    title-prefix: "[test-selection-audit] "
    labels: [area-testing, area-pipelines]
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
    # exact matches against open and recently-closed issues are dropped.
    # Exact (not fuzzy) match: step 1's reconciliation searches for the
    # literal title it wrote to a `pending-filed` row's `note`, and a fuzzy
    # match here could silently dedupe against an unrelated near-duplicate
    # title that reconciliation would never find, stranding the row.
    deduplicate-by-title: true

---

# Weekly CI test-selection audit

Audit Aspire's dynamic test selection for pull requests and find the
**single highest-confidence** case where the selector ran **ALL tests**
unnecessarily or a narrow selection missed a real test consumer. If you
find one, file an issue describing the fix.

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
  because processed PR heads and their counted contributions are retained
  and findings are deduplicated by title. If
  `${{ github.event.inputs.pr_numbers }}` is set, analyze only those PRs
  (ignore the lookback window for both selecting PRs and finding their
  completed CI runs; still use it as context when useful).
- Primary evidence, in order of preference:
  1. The PR's test-selection comment (posted by CI on same-repo PRs).
  2. When no comment matches the current head (fork PRs don't get
     commented on) — the latest relevant CI run for that head: paginate
     `actions_list` through **all pages** of that run's artifacts to find
     `select-tests-selection-Linux` (large CI runs can put it past the
     first page). Download that artifact and read
     `select-tests-selection.json` from its ZIP. The GitHub `actions_get`
     artifact download method returns a temporary ZIP URL, not its
     contents: use `curl --fail --location --silent --show-error
     --max-time 30 --max-filesize 10485760 --output
     /tmp/gh-aw/selection-<run-id>-<attempt>.zip <download-url>` and,
     **only if the download succeeds**, `unzip -p` that ZIP's exact
     `select-tests-selection.json` member. Use the numeric run ID and
     attempt from GitHub, not text from the PR, in the filename. Quote
     the GitHub-provided URL when passing it to the shell; never use a
     PR-authored URL. Do not extract other archive members or trust a
     stale ZIP if the download fails. If the artifact is missing,
     expired, unreadable, or has no selection result, report the data
     gap and leave that PR head unprocessed rather than inventing a
     selection. For a rerun, check the selection job's `run_attempt`
     and `started_at`; an artifact for the same workflow run may be
     left over from an earlier attempt. Use the artifact only when
     its `created_at` falls after this selection job started and its
     workflow run/head SHA match. If no artifact can be attributed
     unambiguously to this attempt, keep the prior record unchanged
     and report the gap. This is the
     authoritative source for fork PRs; do not report a fork PR as "no
     data" just because there is no PR comment.
- If a PR has multiple CI attempts, use the most recent completed attempt
  of the selection job (order by job completion time, not comment
  creation time) only after confirming no newer attempt is still
  pending. A selection
  comment is keyed by head SHA, **not** by attempt; on a new attempt for
  an already recorded head, use that attempt's selection artifact, not
  the comment that may still describe the previous attempt.

## Audit procedure

1. **Load what previous runs already know.** Persistent memory for this
   workflow is mounted at `/tmp/gh-aw/repo-memory/default/`. Read these two
   files if they exist (on the very first run they will not — that is
   normal, treat both as empty and carry on):

   Before anything else, **reconcile any `pending-filed` row** in
   `watchlist.jsonl` (see below): search issues (`search_issues`, any
   state, no date bound) for the exact title stored in that row's `note`.
   If found, rewrite the row to `filed` with that issue's number in `ref`.
   If a `pending-filed` row is still unreconciled after surviving one full
   run this way, the filing did not happen — `create-issue` runs in the
   same workflow run, so a real issue would already exist by the next
   scheduled run — so revert it to `watch` instead of leaving it stuck
   forever; the underlying evidence is not lost, just no longer credited
   as filed.

   - `processed-runs.jsonl` — a **durable index**, one row per resolved
     `pr`+full `sha`, recording the last selection evidence and the exact
     paths credited to that head:
     `{"pr":20131,"sha":"<full head SHA>","run":35802294466,"attempt":2,"all":true,"over_paths":[".github/workflows/build.yml"],"miss_paths":[],"seen":"2026-09-22"}`.
     Use distinct literal paths in each array, not a rule glob or an
     `example_prs` list. An unaffected selection has empty arrays. A
     single head contributes at most **one** to each path's corresponding
     counter, even if it has several CI attempts or several changed files
     matching the same path rule. The `run` and `attempt` identify the
     selection job whose output you used, not the audit workflow's run.

     **`pr` and the full head `sha` are required on every new row.** Key
     on both: a PR gains commits, and the same commit can be reselected
     on another CI run or attempt (including a transient merge-base
     fail-safe becoming a narrow selection on rerun). A comment's
     abbreviated footer is for display; use its linked full commit SHA
     and confirm it equals the PR head. If you cannot establish the full
     SHA, run ID, or attempt, leave the head unresolved rather than write
     an identity that could make a later run skip it.

     Retain these rows even after their `seen` date ages out. This file
     is read in full every run and has a 2 MiB limit: keep rows compact,
     but **never prune or silently omit** an identity to make it fit.
     If a write would exceed the configured size or patch limit, report
     the capacity failure prominently, do not write incomplete ledgers or
     claim exact cumulative counts, and file no issue until the memory
     capacity is explicitly addressed.

   - `watchlist.jsonl` — the rules worth continuing to watch. One row per
     exact triggering path **and kind**, not per rule: broad rules like
     `.github/workflows/**` match files with very different effects, and
     step 6 requires judging each file on its own, so a verdict for one
     matching file must never be reused for another. The same file may
     over-select in one run and under-select in another. Key on `path`
     (the literal file, e.g. `.github/workflows/build.yml`) plus `kind`;
     record which trigger-map rule matched it separately:
     `{"path": ".github/workflows/build.yml", "rule": ".github/workflows/**", "rule_ref": "eng/github-ci/test-trigger-map.yml@a1b2c3d", "path_ref": ".github/workflows/build.yml@e4f5a6b", "kind": "over-selection", "verdict": "watch", "all_runs": 12, "first_seen": "2026-09-08", "last_seen": "2026-09-22", "example_prs": [20131, 20046], "note": "...", "ref": null}`.

     `kind` is `over-selection` (the path escalates to ALL) or
     `under-selection` (the path's rule names `targets` that miss a real
     consumer, per step 7). It picks which counter the row tracks:
     `all_runs` for `over-selection` rows counts escalations to ALL;
     `miss_runs` for `under-selection` rows counts **distinct affected
     PRs/commits** currently credited to that path — never increment it
     just because step 7's static source analysis still finds the same
     gap it found last week, since that gap does not change between runs
     and would otherwise inflate the count every week for zero new
     evidence. Never mix the two counters on one row.

     `rule_ref` is the trigger-map file and the short commit SHA it was
     last read at when this verdict was set. `path_ref` is the *triggering
     path itself* and the short commit SHA it was last read at — track
     both, since a `correct-by-design` verdict for a workflow/action often
     turns on what that file currently runs (step 6's self-referential
     judgment, or the single-job-gate case in step 4), not just on the
     trigger-map rule that selected it; if the workflow later changes what
     it gates while the trigger-map rule stays untouched, `rule_ref` alone
     would look unchanged and the stale verdict would suppress the path
     indefinitely. Before carrying a `correct-by-design` verdict forward,
     confirm **both** SHAs still match the paths' current commits — if
     either the trigger-map rule or the triggering path itself has moved
     since it was recorded, treat the verdict as stale and re-derive it
     from scratch rather than trusting an out-of-date read.

     `verdict` is one of:

     - `watch` — a plausible candidate that has not yet cleared the
       confidence bar. Keep accumulating evidence for it.
     - `correct-by-design` — settled; stop re-deriving it.
     - `pending-filed` — this run asked `create-issue` to file it, but
       `create-issue` runs in a separate job after this one finishes, so
       the agent never learns the resulting issue number or whether
       filing even succeeded (it can be silently dropped by
       `deduplicate-by-title`, or fail if the assignment PAT is missing).
       Put the **final** issue title in `note` — the exact string
       `create-issue`'s `title-prefix` (`[test-selection-audit] `) plus
       the title you chose, matching what the real issue will actually
       be titled — so the next run's reconciliation search can find it.
       A `note` missing that prefix will never match, and the row will
       keep reverting to `watch` and re-attempting the same filing every
       run. Do not write `filed` directly — there is no issue number to
       put in `ref` yet.
     - `filed` — a prior `pending-filed` row was confirmed against a real
       issue (see step 1). Put the issue number in `ref`.
     - `in-flight` — someone else is already fixing it (see step 9). Put
       the PR number in `ref`. Do not record this as `filed`: the two
       decay differently, since an in-flight PR can be closed unmerged and
       the rule then returns to `watch`, whereas a filed issue stays ours.
     - `fixed` — the referenced fix has merged. Retain the row and its
       historical counters so a later CI attempt can reverse a credited
       head's prior contribution. Put the merged fix's PR number in `ref`;
       re-evaluate if the rule changes again.

     Use `correct-by-design` for anything the prompt tells you to reject as
     intended behavior rather than as weak evidence — a file on the
     build-input list in step 5, or a self-referential selector change in
     step 6. Those are settled, not still being watched.

     Only create a row for a path actually observed escalating (or
     missing a consumer, for `under-selection`) this window. Do not seed
     a row for a path you merely noticed sharing a fix with an observed
     one. Retain a previously credited row even if its count falls to
     zero after a rerun; the prior finding and its verdict are still
     part of the audit history.

     Carry these forward rather than re-deriving them. For a row recorded
     `correct-by-design`, skip re-reading the trigger map and the
     triggering path's history only after confirming **both** `rule_ref`
     and `path_ref` are still at the commit SHA recorded there (a cheap
     `list_commits`/`get_file_contents` check per path, not a full
     re-derivation); if either the trigger-map rule or the triggering path
     has moved since it was recorded, re-derive the verdict and update
     whichever `*_ref` changed. Otherwise apply only the per-head
     contribution changes from step 13; an unchanged rerun adds nothing.

   **Rows written by an older version of this prompt may not match the
   shapes above.** Never delete or rewrite a row just because its shape is
   unfamiliar, and never invent a missing field to make one conform. Treat
   an unrecognized field as extra detail and ignore it; treat a missing
   field as unknown. In particular, a row with no `sha` cannot prove
   which head was resolved; do not use it to skip any head. For a short
   `sha` that matches the listed head's prefix, or a row without `run`,
   `attempt`, or contribution arrays, do not add another count or
   subtract a contribution you cannot identify. Report the uncertain
   historical count and exclude it from a candidate's confidence claim
   until the legacy entry can be reconciled against evidence. Preserve
   legacy rows and totals; never silently reset them. New heads that
   do not match a legacy identity can use the complete row format.

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
     to write the ledger later. **Enumerate all PR states, not just
     open** — `list_pull_requests` defaults to `state: open`, but most PRs
     in a multi-week window are already merged or closed, and their
     completed selection runs are exactly the evidence this audit exists
     to accumulate. Pass `state: all` (or issue separate `open`/`closed`
     calls) and bound the set by the window using each PR's own
     created/updated/merged timestamp — do not rely on API result
     ordering alone to decide when to stop paging. Do **not** skip a PR
     merely because its current head SHA has a processed row. First
     compare the head's **latest relevant selection run ID and attempt**
     with the recorded values. An unchanged completed attempt needs no
     comment or artifact calls. A newer attempt on the same head must be
     re-evaluated, not skipped; if it is pending, approval-blocked, or has
     no usable selection evidence, keep the prior row and contributions
     unchanged and report the gap. For an unrecorded head, check run
     status/conclusion before trusting a head-matching comment. Then read
     the **selection comment** for remaining unrecorded heads to decide
     whether they selected `ALL`; if the comment cannot be tied to the
     latest completed attempt (for example, there was a rerun), use that
     attempt's artifact instead. Retain changed paths already included
     in a narrow-selection comment as potential step 7 evidence; fetch
     changed files only for a specific under-selection candidate, not
     every normally selected PR in this pass. If the selection job did
     not rerun when another job in the same workflow was rerun, that is
     not new selection evidence; keep the recorded selection unchanged.

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
     re-runs, so a PR can carry several marked comments. Do not take
     whichever is newest by timestamp: out-of-order CI completions (a
     later push's run finishing before an earlier push's) and a force-push
     back to an already-commented SHA (which updates that comment in
     place, not its position) both make "most recent" point at a
     superseded commit even though the real result already exists. Instead,
     match by the footer SHA itself: find the marked comment whose footer
     names the PR's current head SHA (from pass 1's listing). Never use
     a comment for a different SHA as evidence for the current head.
     If none match, follow the no-matching-comment run check below;
     zero comments is normal for forks, not a reason to skip their CI.

     When a matching comment exists, use its footer SHA for the ledger;
     when the result comes from a CI artifact, use that run's head SHA.
     In either case it must equal the PR's listed current head SHA.
   - *Pass 2 (narrow, detailed).* Only for PRs that selected `ALL`, capture:
     PR number, run ID, attempt, timestamp, changed files, selected
     project/test count, the escalation reason, and any
     unmatched/unattributed file that caused the escalation.

   **Skip, without spending further calls on them**, any PR that cannot have
   a selection result yet:

   - CI still pending — the latest run's `status` is `queued` or
     `in_progress`. The selection may not be posted yet, or may still
     change on a later attempt.
   - CI blocked on approval — a fork PR waiting on maintainer approval
     before workflows run. This is `action_required`, a run *conclusion*,
     not a status: the run's `status` is already `completed` (there is
     nothing in progress to wait on), so check `conclusion ==
     "action_required"`, not `status`. Checking `status` alone treats an
     approval-blocked run as an ordinary completed run with no selection
     output, which is a false gap, not a real one.
   - No CI run in the lookback window (only when `pr_numbers` is empty),
     or the selection job did not run. For an explicit `pr_numbers`
     dispatch, inspect the specified PR's latest relevant run for its
     current head even when that run predates the lookback window; if
     its artifact has expired, report the gap without inventing a result.

   These are not findings. Do not read an artifact for a pending or
   approval-blocked attempt even if an older head-matching comment exists;
   that comment may be stale. Preserve any recorded result until a newer
   completed attempt has usable evidence. Count skips in the run summary
   so a quiet window is distinguishable from one with no `ALL` selections.

   A PR with no **head-matching** marked comment is ambiguous on its own:
   a stale comment proves nothing about the current head, and a fork PR
   may never get one regardless of CI state. Check the latest CI run for
   the **current head SHA**, including its `status` and `conclusion`,
   before deciding which bucket it falls in:

   - If `status` is queued/in-progress or `conclusion` is
     `action_required`, skip it per the list above, without recording
     this head as processed.
   - If the run completed and the selection job ran, read its selection
     artifact as described under Primary evidence. This is expected for
     forks; for same-repo PRs, report the missing head-matching comment
     as a gap but still use the artifact rather than silently skipping
     a completed selection. For a newer run/attempt on a previously
     recorded head, always use that attempt's artifact: a comment's SHA
     footer cannot attest to which attempt wrote it.
   - If no selection job ran, or its artifact is unavailable, skip the
     unresolved head and report the data gap. Do not add or replace its
     row in `processed-runs.jsonl`.
3. **Classify.** Group `ALL` selections by the triggering file/path/rule.
   Quantify frequency (how many PRs/runs hit each trigger) and keep 2-3
   concrete example PRs per trigger.

   First, exclude any selection whose `escalationReason` is the
   `run-full-ci` label kill switch (`"kill switch: the run-full-ci label
   forces the full matrix"`, or a caller-supplied override of that same
   switch — see `tools/SelectTests/TestSelector.cs`). That is a human
   deliberately forcing the full matrix, not a trigger-map defect;
   treat it as `correct-by-design` and never count it toward a rule's
   escalation total. This is distinct from the merge-base fail-safe
   fallback, which uses its own reason text and is real evidence — do not
   over-broaden this exclusion to match on "kill switch" or `ForceAll`
   generically.

   Stage each resolved head's distinct `over_paths` as proposed
   contributions, and report the cumulative figure from `watchlist.jsonl`
   alongside this window's. Do not increment the watchlist here:
   step 13 applies the difference from that head's prior contributions
   **after** step 7 determines `miss_paths`. An unchanged rerun contributes
   nothing new; an ALL-to-narrow rerun must remove its previous ALL
   contribution. A merge-base fail-safe ALL result has no triggering path
   and must not be attributed to a rule just to make the totals grow.
4. **Prefer safety over CI savings.** Do not propose narrowing a selection
   unless the file's real consumers are known and either existing tests
   already cover the invariant, or a focused guard test could be added that
   would fail if the narrowed behavior regressed. A missed test is worse
   than an extra CI run — when in doubt, do not propose narrowing.

   An existing ALL-route with a rationale that names a *general* effect
   (e.g. "checkout normalization can affect fixture-sensitive tests")
   rather than a specific, already-covered test is not proof the ALL scope
   is the minimum safe one — it usually means nobody has written the guard
   yet. That guard must be exhaustive, not just pin today's known values: a
   test that only asserts the file's *current* directives/values stay
   unchanged does not catch a new, unguarded directive being added, and a
   new addition is exactly the class of edit a narrower route needs to
   catch. For example, a guard pinning `.gitattributes`'s existing rules
   stays green when a PR adds a brand-new `*.cs` rule, because it only
   ever inspected the rules it already knew about — yet that new rule
   changes every checkout of a `.cs` file, and a narrower route would
   silently miss it. The guard must instead assert the file's *complete*
   set of directives against an explicit allow-list and fail on anything
   outside it — including a new directive of the same byte-affecting kind
   (e.g. another `text`/`eol`/filter attribute) — not just re-check the
   values already known. If you cannot write a guard with that exhaustive,
   reject-anything-new shape, keep the path routed to `ALL` instead of
   proposing the narrower route.
5. **Do not question broad build-input files.** These files legitimately
   affect nearly the entire .NET project graph — treat their `ALL`
   escalation as correct-by-design and do not flag it as a finding, even if
   it looks broad:

   `Directory.Packages.props`, `Directory.Build.props`,
   `Directory.Build.targets`, `NuGet.config`, `eng/Versions.props`,
   `eng/Version.Details.xml`, `src/Directory.Build.props`, and
   `global.json`.

   **That list is exhaustive.** It is not a category to reason by analogy
   from. A file is exempt because it appears above, not because it sits at
   the repository root, has a `.props`/`.json` extension, or looks
   infrastructural. In particular, `.gitattributes`, `.editorconfig`,
   `.config/dotnet-tools.json`, `Aspire.slnx`, and CI YAML are **not** on
   this list and must be judged on their actual effect like anything else.
   A previous run wrongly waved `.gitattributes` through as a "broad
   build input", and separately put `Aspire.slnx` on this very exemption
   list by analogy ("it's a solution file, sounds build-wide") — but
   Layer 1 already roots its project graph at `Aspire.slnx` at the PR
   head, so an added project is already in the universe the graph walks
   and carries no signal the graph does not already have; only the
   removal direction still needs the run-all fallback, since a deleted
   project's files can no longer be attributed to anything. Both are
   exactly the kind of over-selection this audit exists to catch — do
   not let a file's *name* substitute for reading what actually consumes
   it.
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
7. **Also look for under-selection, not just ALL.** A rule that already
   names specific `targets` can still be wrong in the opposite direction:
   its target list can be narrower than the file's real consumers, so a
   change silently runs too few tests instead of falling back to ALL. This
   is the more dangerous failure mode, because nothing in the selection
   comment looks anomalous — the selector reports a confident, narrow
   selection, and a missed test does not show up as a `.gitattributes`-style
   over-broad rule would. Treat a candidate here with **at least** the same
   rigor as an over-selection one, and weigh it higher when both are
   equally well-evidenced: a missed test risks a real regression escaping,
   where an extra CI run only costs compute.

   This check is not driven by which PRs selected ALL this window — an
   under-selecting rule never shows up that way. Instead, read the trigger
   map's `path_rules` / `affected_project_rules` / `derived_targets`
   entries whose paths fall under the repository's highest cross-cutting
   surfaces, where a change ripples into multiple consumers and a stale or
   incomplete target list would let a regression through untested:

   - `src/Aspire.Hosting/**` — core orchestration APIs every hosting
     integration and the CLI's generated-AppHost path build on.
   - `src/Aspire.TypeSystem/**` and `src/Aspire.Hosting.CodeGeneration.*/**`
     — changes ripple into every polyglot language exporter (Go, Java,
     Python, Rust, TypeScript) and the generated SDK contract.
   - `src/Aspire.Dashboard/**` — Blazor components plus their JS interop.
   - `extension/**` — the VS Code extension (bootstrap, RPC bridge, e2e).
   - the CLI (`src/Aspire.Cli/**`, acquisition scripts, native archive
     packaging).

   For each such rule, independently enumerate the path's real consumers
   from source — grep for project references, generated-code call sites,
   or RPC/protocol message types it defines — rather than trusting the
   rule's `reason` comment to already be complete. Most compiled C#
   dependencies here are Layer 1's job (the project graph is exhaustive
   for MSBuild project references) and do not need this check; focus on
   exactly the blind spots Layer 2 exists to cover — a runtime-only
   dependency such as a package loaded by `aspire add`, a generated
   AppHost, a fixture copied into an E2E workspace, or a contract read by
   a codegen target that Layer 1's static graph cannot see. If you find a
   consumer the rule's targets omit, name the missing target and cite the
   specific reference (file:line) that proves the dependency — the same
   standard of evidence step 11 requires for an over-selection candidate.
   Once a gap is confirmed, find distinct affected PR heads in this
   window from the changed paths retained in pass 1's comments or
   selection artifacts; fetch changed files for promising narrow-result
   PRs only when that evidence is absent. When re-evaluating a head,
   also check every path previously in its `miss_paths`, even if it
   would not be considered as a new candidate this run. Stage the
   matching literal path in that head's `miss_paths` when its selection
   omitted the proven consumer, whether this is the first selection or
   a newer attempt for a previously processed head. Compare with its prior
   `miss_paths` in step 13; do not use `example_prs` to deduplicate
   counts, because it does not track SHAs. A static map
   omission without a matching new PR is still worth reporting in the
   run summary, but supplies neither a `miss_runs` increment nor the
   concrete example required to file an issue.
8. **Verify against source, not memory.** For every candidate, read the
   actual selector implementation, `eng/github-ci/test-trigger-map.yml`,
   `docs/ci/test-trigger-map.md`, and the real changed-file list from the
   example PRs before concluding the selection is wrong. Do not speculate
   about what a file "probably" affects.
9. **Check whether a fix is already in flight.** Before going further with a
   candidate, check whether someone is already fixing it:

   - `search_pull_requests` only searches issue-style metadata (title,
     body, labels) — it cannot see a PR's changed files, so an open PR
     that edits the trigger map without naming it or the rule in its
     title/body would be missed. List open PRs
     (`list_pull_requests`, `state: open`) and check each one's changed
     files (`get_pull_request_files`) for `eng/github-ci/test-trigger-map.yml`;
     use `search_pull_requests` in addition, for PRs that name the rule by
     text but might not (yet) touch the file.
   - Check `watchlist.jsonl` for an `in-flight`, `filed`, or `fixed`
     verdict against this exact `(path, kind)` by an earlier run.
     Confirm whether a referenced PR is still open, closed unmerged,
     or merged, or a filed issue still tracks the fix. An open issue,
     open PR, or merged fix that still applies blocks a duplicate.
     Reopen `watch` if an in-flight PR closed unmerged or a prior fix
     no longer applies to the current rule.

   If a fix is already tracked or merged, reject the duplicate and say so
   in the run summary. Filing anyway would start a second coding agent on work
   that is already done and put a duplicate PR in front of a reviewer.
10. **Check how this was handled before.** Maintainers have already made
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
11. **Apply the confidence bar.** Candidate findings include: a path rule
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
   - You can name a test that would fail if the narrowing were wrong,
     including the exhaustive new-directive guard step 4 requires for
     a byte-affecting file.
   - If an existing guard test currently pins the behavior you want to
     change, you can state how that test's contract should change.
   - History does not show this same narrowing already being tried and
     reverted.
   - You would be comfortable defending the change in review.

   If any of those is missing, it is not high-confidence. Report it in the
   run summary instead.
12. **Pick one, or none.** If several candidates clear the bar, file only the
   strongest — prioritize a proven missed consumer over comparable CI
   savings, then weigh the clarity of the evidence and the number of
   affected PRs or avoidable `ALL` runs. If none clear it, file nothing.
   A run that files no issue is a
   normal, successful run; filing a weak finding is worse than filing
   nothing, because it starts a coding agent session and consumes human
   review time.
13. **Write back what you learned.** Before finishing, update the two
    ledgers in `/tmp/gh-aw/repo-memory/default/`. They are committed
    automatically after the run; you only need to write the files.

    - For every head with a **new, verified** selection result this run,
      finish both over- and under-selection checks before updating either
      ledger. Compare its staged distinct `over_paths`/`miss_paths`
      against that exact `pr`+full `sha` row's arrays **as they existed
      at the start of this run** (empty sets for a genuinely new head).
      For each `(path, kind)`, apply `+1` only if newly credited and `-1`
      only if previously credited but now absent; unchanged sets have
      delta zero. Apply all deltas to the corresponding `watchlist.jsonl`
      `all_runs` or `miss_runs` fields, then replace the old processed
      row with this run ID, attempt, result, path arrays, and `seen` date
      (or append the row for a new head). Count a head once per path,
      never once per CI attempt. Check that no counter becomes negative
      and that a row exists for every previously credited path; if either
      check fails, report the inconsistency and leave **both** ledgers
      unchanged rather than guessing a correction.

      If a newer attempt is pending, blocked, has no selection job, or
      lacks an attributable selection artifact, do not replace the head's
      prior row, adjust its counters, or treat an old SHA-matching
      comment as current evidence. Do not write rows for unresolved
      heads. Preserve older rows **indefinitely**: PR `updated` time can
      bring an old head back into a scheduled window, and `pr_numbers`
      can revisit one at any age. Pruning by `seen` would turn it into
      a fresh head and add its existing contribution a second time.
    - Update `watchlist.jsonl` using those per-head deltas, plus any
      carried-forward rows. An unchanged rerun or repeated static gap
      has no delta: it must not refresh `last_seen` or increment a
      counter. Refresh `last_seen` only on a newly credited observation;
      on retraction, keep it as the historical last-observation date,
      not an assertion that the retracted evidence is still credited.
      Keep `example_prs` drawn from currently credited heads rather
      than retaining a PR whose only contribution was retracted.
      Preserve existing `verdict` and `ref` when replacing evidence;
      change them only after separately verifying that the underlying
      rule or fix changed. Keep `watch`
      and `correct-by-design` rows even when their counts fall to zero.
      Once a fix merges, mark the row `fixed` instead of deleting it:
      old heads still reference its counts and may need corrections.

    No JSONL-aware merge protects either file: a rejected push retries
    with `git pull --no-rebase -X ours`, and **even two appends at EOF
    can conflict**, silently dropping one run's rows. The workflow-level
    concurrency group serializes the agent and memory-push jobs; do not
    rely on the file format to make concurrent runs safe. When updating
    a row, preserve every unrelated row. Verify both ledgers fit the
    configured file and patch limits **before** emitting `create-issue`;
    on failure, report the capacity problem and make no partial update.
    Keep rows one-line and minimal; both files are read in full on every
    future run.

## The issue you file

Write the issue as a task specification for the Copilot coding agent that
will be assigned to it — it should be able to start work from the issue
alone, without re-doing your analysis.

Title it so it names the offending rule or input, for example
`Narrow <rule/path> so it no longer escalates test selection to ALL` for
an over-selection finding, or
`Add <consumer> to <rule/path>'s targets so <test project> is selected`
for an under-selection one.
Titles are deduplicated against open and recently-closed issues, so a stable,
specific title prevents re-filing the same finding on a later run.

The body must contain:

- **Symptom**: the concrete finding — which PR(s)/run(s), what changed,
  and either that the selector ran ALL tests as a result (over-selection)
  or which real consumer's tests the narrow selection missed
  (under-selection). Quote the selection reason/log line verbatim in a
  fenced code block.
- **Evidence**: real example PR(s) that hit this rule, each with the
  file(s) it touched and a before/after project count — how many test
  projects ran under the current rule versus how many would run under
  your proposed fix. You cannot run `tools/SelectTests` yourself in this
  sandbox, so derive this count by reading the trigger map's rules and
  targets directly (which rule newly does or no longer matches, and which
  targets that adds or removes) and label it explicitly as an estimate;
  the assigned agent establishes the exact count under Required
  validation below. **One clear, unambiguous example is enough** — do not
  pad the issue with additional PRs just to hit a count. Reach for more
  than one only when a single example leaves genuine room for doubt (for
  example, it could plausibly be a one-off rather than a repeating
  pattern); in that case, pull the extra examples and the cumulative count
  from `watchlist.jsonl`'s history for this rule rather than searching for
  new ones. A rule that has been climbing for weeks is stronger evidence
  than one seen once — say which case this is. Link the specific
  trigger-map rule or code path responsible, with file and line.
- **Root cause**: why the current rule is broader (or narrower/missing)
  than necessary, in one or two plain-language sentences. This is the
  sentence a reviewer should be able to quote back to explain the change
  in one breath — write it so it stands on its own, without the reader
  needing the rest of the issue.
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
  - Run `tools/SelectTests` against the changed-file lists from **every**
    example PR named in Evidence, not just one, and report selected-project
    counts before and after the change for each.
  - Run the guard tests:
    `dotnet test --project tests/Infrastructure.Tests/Infrastructure.Tests.csproj --no-launch-profile -- --filter-namespace "*.TestTriggerMap" --filter-not-trait "quarantined=true" --filter-not-trait "outerloop=true"`
  - Open the result as a **draft** PR that links back to this issue.
- **PR description requirement**: tell the assigned agent explicitly that
  the PR it opens must carry this issue's Root cause sentence and the
  Evidence examples forward into its own description, not just a diff
  summary — state it in the issue body as an instruction the assigned
  agent will follow, e.g. "Your PR description must restate why the old
  rule was wrong and name the real PRs this would have helped, with their
  before/after counts, so a reviewer can judge the change without
  re-deriving your analysis." A reviewer approving a trigger-map change
  should not have to re-open this issue to find out why.
- **Unvalidated-analysis caveat**: state that this issue came from an
  automated audit and the suggested fix has not been validated by running
  the selector or tests. If validation contradicts the analysis here, the
  assigned agent should say so on the issue and not force the change
  through.
- Footer: `<sub>Automated by the weekly CI test-selection audit workflow.</sub>`

## Run summary (always report, regardless of whether an issue was filed)

In your final response, report:

- How many PRs/runs were analyzed and over what window (or which PR numbers,
  if explicitly given), and how many heads reused a recorded result
  after a current-run metadata check without re-fetching selection
  comments or artifacts. Report separately any reruns that replaced
  prior contributions and any missing/stale-attempt evidence.
- How many PRs were skipped because they could not have a selection result
  yet (CI pending, `action_required`, or no selection job), so a quiet
  window is distinguishable from an unanalyzable one.
- Total selection runs seen, how many were `ALL`, and the top `ALL` triggers
  with counts — both for this window and cumulatively across runs.
- The current watchlist: each path being tracked, its cumulative `all_runs`
  / `miss_runs` count, and how that count moved this run. A path whose
  count is climbing week over week is the audit's main product even when
  nothing is filed.
- Candidates you considered but rejected as correct-by-design or as failing
  the confidence bar, and which specific criterion each one failed. Call out
  separately any candidate rejected because history shows the same change
  was already tried and reverted, and any rejected because a fix is already
  in flight.
- The issue filed this run, if any, or a one-line note that no finding
  cleared the bar this week.
