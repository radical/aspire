# CI Failure Shepherd Design

## Summary

Aspire's CI automation currently files two related classes of issues:
`ci-failure-cause` issues created by agentic failure analysis and
`automation-broken` issues created by several deterministic workflow
reporters. These systems use different matching and lifecycle rules. Some
issues represent a single root cause, while others accumulate unrelated
failures, reopen after a human closes them, or remain open after their fix
has merged.

The first CI failure shepherd will be a user-level Copilot skill run manually
on a developer machine. It will collect authoritative GitHub evidence,
classify every relevant issue, and propose the next action without modifying
GitHub. Each run will produce a concise chat summary and detailed local
Markdown and JSON reports.

The prototype deliberately separates deterministic fact collection from
agent judgment. This preserves reproducibility, makes missing evidence
visible, and allows the collector to become a foundation for later scheduled
or write-enabled versions without making CI correctness depend on an agent.

The producer-aware lifecycle preparation and candidate-authority refinement is
specified in
[CI Shepherd Lifecycle Hardening Design](2026-08-19-ci-shepherd-lifecycle-hardening-design.md).
That refinement supersedes this document where it narrows the assessment agent
to bounded evidence bundles and downgrade-only decisions. GitHub access remains
read-only.

## Goals

The prototype will:

- Inventory all open issues labeled `ci-failure-cause` or
  `automation-broken`.
- Consult relevant recently closed issues to detect duplicates, resolved
  causes, and regressions without reopening historical incidents.
- Gather issue, run, timeline, pull request, commit, and ownership evidence
  into a deterministic normalized snapshot.
- Assign each issue one lifecycle state and one proposed action.
- Explain every proposed action with source links, confidence, reasoning, an
  objective next condition, and a suggested owner when appropriate.
- Treat `ci/main` issues as incident records and route recurring flaky-test or
  infrastructure defects toward separate canonical problem issues.
- Compare each run with the previous run and emphasize changes requiring
  attention.
- Produce stable machine-readable output suitable for later evaluation and
  automation.
- Remain read-only until report quality and safety have been established.

## Non-goals

The prototype will not:

- Comment on, edit, label, assign, close, reopen, or create GitHub issues.
- Push fixes, create branches, or create pull requests.
- Replace deterministic issue creation, exact deduplication, or close-on-green
  behavior in GitHub workflows.
- Redesign the existing workflow reporters in the same implementation.
- Treat an LLM-generated phrase or slug as a durable failure identity.
- Infer that a failure is resolved only because an issue or pull request is
  closed.
- Require the shepherd to run for CI reporting to remain operational.

## Installation and Invocation

The prototype will be installed as a user-level Copilot skill named
`ci-shepherd`. Its collector and supporting fixtures will be bundled with the
skill so the initial experiment does not modify Aspire's operational
workflows.

The target checkout and the installed skill root are separate paths. From the
target checkout, preserve `CHECKOUT` before invoking the collector from the
directory that contains `SKILL.md`:

```bash
CI_SHEPHERD_ROOT="/path/to/directory-containing-SKILL.md"
CHECKOUT="$PWD"
SCRATCH="$HOME/.copilot/ci-shepherd/manual-run"
umask 077
mkdir -p "$SCRATCH"
python3 "$CI_SHEPHERD_ROOT/scripts/collect.py" \
  --repository microsoft/aspire \
  --checkout "$CHECKOUT" \
  --output-dir "$SCRATCH"
```

Validation likewise runs from
`"$CI_SHEPHERD_ROOT/scripts/validate.py"` after the agent writes
`"$SCRATCH/report.json"`. Neither script is resolved relative to the target
repository.

A manual invocation will identify the target repository, defaulting to
`microsoft/aspire`, and optionally accept:

- A local Aspire checkout to consult for CODEOWNERS and source history.
- A lookback period for recently closed issues and workflow runs.
- An output root.
- A previous run to compare, defaulting to the latest successful snapshot.
- A fixture input for deterministic tests and corpus evaluation.

Authentication will use the existing `gh` CLI session. The skill will verify
that the token has read access to issues, actions, pull requests, commits, and
repository contents before collection begins. No write scopes are required.

## Architecture

The skill contains four bounded components.

### Collector

The collector retrieves GitHub facts and writes a normalized snapshot. It
does not diagnose failures, choose owners, or recommend actions.

Its responsibilities are:

- Query the union of open `ci-failure-cause` and `automation-broken` issues.
- Query recently closed issues with either label as a candidate index, then
  enrich only a bounded supporting set.
- Fetch comments for included issues. The runnable default profile leaves issue
  timelines disabled.
- Extract and verify references to workflow runs, pull requests, commits,
  issues, tests, jobs, steps, and branches.
- Fetch bounded referenced workflow-run metadata, failed jobs, available
  failure logs, and recent workflow history.
- Fetch referenced pull request and commit state.
- Read CODEOWNERS and relevant repository history when a local checkout is
  available.
- Record collection failures and evidence expiry explicitly.

The collector will use `gh api` and documented GitHub REST endpoints rather
than scraping rendered issue pages. Pagination, rate-limit handling, and
reference normalization remain deterministic. Every GitHub request made by the
prototype is a GET.

The runnable `scripts/collect.py` profile is intentionally smaller than the
collector library's compatibility defaults. It admits at most 20 supporting
closed issues, follows at most five explicit issue references per issue, and
retains at most three marker candidates and three normalized-fact candidates
per open issue. Deterministic warnings name every budget that truncates
candidates. This preserves bounded canonical evidence without restoring the
old eager crawl.

These are endpoint-family/result budgets, not HTTP-call guarantees: at most 20
supporting issue candidates receive enrichment, and at most 10 selected
referenced runs receive one first-page history request each. Issue detail and
comment endpoints may paginate. The client pagination loop stops after a page
with fewer than 100 results but has no fixed page-count cap, while
`.ci-shepherd-build/scripts/ci_shepherd/github.py` permits at most three
attempts for each page, detail, or log request. Pagination and retry traffic are
therefore bounded separately by the client behavior and are not counted as one
call per candidate. The profile does not claim a total HTTP-request upper
bound.

### Normalizer

The normalizer converts API responses into stable records. Volatile ordering,
redundant GitHub payload fields, and formatting differences are removed so
equivalent evidence yields equivalent JSON.

The normalizer also derives factual relationships that do not require
judgment, including:

- Label-set membership.
- Exact issue marker matches.
- Explicit cross-references.
- Run-to-commit and run-to-branch links.
- Pull request merge state and merge commit.
- Issue close and reopen episodes.
- Comment authorship and timestamps.
- Whether linked logs or artifacts are available, expired, or inaccessible.

It may extract candidate failure facts such as a test name, exception type,
error code, failing job, and failing step. These remain evidence fields, not
the final semantic classification.

### Shepherd

The shepherd consumes only the normalized snapshot plus explicitly gathered
local repository evidence. It:

- Groups issues that may describe the same root cause.
- Distinguishes incidents, root-cause bugs, broad status trackers, and
  transient occurrences.
- Selects a lifecycle state and proposed action.
- Assesses confidence and identifies contradictory or missing evidence.
- Suggests owners based on verified ownership and change history.
- Defines the objective condition for the next transition.
- Compares the decision with the previous report.

The shepherd must cite snapshot evidence for every material claim. It must not
fill missing facts with plausible defaults.

### Reporter

The reporter validates the shepherd output against the report schema, writes
the JSON and Markdown reports, updates the local latest-run pointer, and
renders the concise chat summary.

The reporter, rather than the shepherd prose, determines ordering and required
sections. This keeps reports comparable between runs.

## Local Data Layout

Run data will be stored outside the repository:

```text
~/.copilot/ci-shepherd/microsoft-aspire/
  latest.json
  runs/
    2026-08-17T210328Z/
      manifest.json
      input.json
      report.json
      report.md
      collection-errors.json
```

`manifest.json` records the collector version, schema versions, repository,
invocation parameters, collection start and end times, GitHub API identity,
local checkout commit when used, and completion status.

`input.json` is the normalized factual snapshot. `report.json` is the
machine-readable shepherd result. `report.md` is the detailed human report.
`collection-errors.json` contains partial collection failures even when the
run can continue.

`latest.json` points to the most recent fully reported run. It is updated only
after snapshot and report validation succeeds, so a partial or interrupted run
cannot replace the comparison baseline.

Raw logs may contain sensitive diagnostic data and will remain local. The
prototype will not upload reports or artifacts.

## Collection Scope

### Open issues

Every open issue with either target label is included. If an issue has both
labels in the future, it appears once with both labels recorded.

### Recently closed issues

Closed issues are supporting evidence, not primary work items. The collector
builds candidates from:

- Issues with either target label closed within the configured lookback.
- Closed issues explicitly referenced by an open issue, linked run, pull
  request, or commit.
- Closed exact-marker matches found by deterministic workflow metadata.
- Closed issues returned as candidate matches for a normalized failure fact.

The default lookback is 90 days. Explicit issue references are followed
regardless of age, to a maximum depth of two and within the per-issue and
global budgets. Explicit references are prioritized when the 20-issue global
supporting budget is exhausted. The collector emits deterministic warnings for
reference, marker, fact, and global-supporting truncation. The global cap is
applied before probing out-of-lookback issue details; skipped references remain
explicit `not-enriched` evidence and make the originating search incomplete.
References discovered beyond the depth limit behave the same way. An excluded
inventory reference carries deterministic `supportingSelection` metadata:

```json
{
  "state": "excluded",
  "reasons": ["depth-limit"],
  "rootIssueNumbers": [21]
}
```

The other exclusion reason is `global-budget`. Generic GitHub enrichment must
honor either reason and must not fetch an excluded issue later. A partial
`not-enriched` evidence stub preserves the issue identity and a `referencedBy`
association to every affected open root.

Each open issue and its issue evidence record carries:

```json
{
  "supportingSearch": {
    "complete": true,
    "candidateIssueNumbers": [],
    "truncated": false
  }
}
```

`complete` is false when a relevant collection error, depth limit, or budget
truncation could hide a candidate. `truncated` is true for either deterministic
selection limit, and `candidateIssueNumbers` contains only selected candidates.
An empty list with `complete: true` is therefore distinguishable from a missing
or incomplete search.

Every selected supporting issue's issue evidence record merges a deterministic
`referencedBy` association for each open issue whose explicit reference,
marker match, or fact match caused selection. Direct explicit references retain
the originating source evidence association. When an aggregate marker or fact
match has no single originating evidence ID, the association uses the stable
open issue evidence ID, its `sourceIssueNumber`, and extraction method
`marker-match` or `fact-match`. Associations are deduplicated and sorted, so a
supporting issue selected for multiple open issues remains usable by each
issue-scoped validator.

### Workflow runs

The runnable default selects at most 10 explicitly linked runs from included
issues. For each selected run it makes one bounded same-workflow, same-branch
first-page history request with `per_page=10` and retains at most the 10 newest
normalized entries. It never follows history pagination and does not download
every repository workflow run.

For each run it records:

- Workflow identity, event, branch, commit, attempt, conclusion, and timing.
- The current attempt and at most 10 failed jobs.
- At most three available failed-job logs, or precise unavailability reasons.
- Rerun relationships.
- Referenced issue markers and failure records when present.
- `recentHistory`, `recentHistoryCollected`, `recentHistoryTruncated`,
  `recentHistoryTotalCount`, `historyCoversSourceRun`, and `recentHistoryGap`.

The full enrichment API retains its prior behavior for callers that do not opt
into the runnable minimal profile. The explicit `include_run_history` option
lets that profile combine minimal job/log collection with one bounded history
request. A failed history endpoint or missing workflow/branch identity records
`recentHistoryCollected: false` and a collection gap. Missing source run
identity or timestamps, malformed history responses, and endpoint errors do the
same.

`historyCoversSourceRun` is true only when the bounded first page proves that
all runs newer than the source run are present. The proof holds when the source
run itself appears in the returned window. It also holds when the complete
history is known to fit in the window: a reported total at most 10 agrees with
the returned count, or fewer than 10 results are returned when no total is
available. Exactly 10 results without a total is conservatively potentially
truncated. A source run older than a truncated window is therefore not covered.

### Pull requests and commits

Explicitly referenced pull requests and commits are verified against GitHub.
For a proposed fix, the snapshot records whether the pull request is open,
closed, or merged; its merge commit; affected paths; linked issues; and
relevant post-merge runs.

### Ownership

The collector reads current CODEOWNERS rules and resolves them against
evidence-backed affected paths. It may also gather recent authors for the
specific workflow, test, or product path and authors of a linked fix.

The last commenter alone is not ownership evidence.

## Input Schema

The exact implementation format may use generated types, but `input.json`
must preserve these stable concepts:

```json
{
  "schemaVersion": 1,
  "repository": "microsoft/aspire",
  "collectedAt": "2026-08-17T21:03:28Z",
  "issues": [],
  "runs": [],
  "pullRequests": [],
  "commits": [],
  "ownership": [],
  "collectionErrors": []
}
```

Each issue record includes:

- Number, URL, title, body, author, state, labels, assignees, and timestamps.
- Comments in chronological order.
- Close and reopen timeline episodes.
- Explicit references and deterministic tracking markers.
- Extracted candidate failure facts with their source locations.
- Evidence availability and per-resource collection errors.

Each extracted fact includes the original value, normalized value, source URL,
and extraction method. This makes normalization inspectable and prevents a
derived value from being mistaken for raw GitHub evidence.

Each evidence record contains `kind`, `url`, `collectedAt`, `availability`, and
a factual `payload`. The supported availability values match collector output:
`available`, `partial`, `expired-or-unavailable`, and `not-enriched`. Unknown
values invalidate the snapshot. The collector does not classify factual
records into semantic action roles.

## Output Schema

`report.json` contains:

```json
{
  "schemaVersion": 1,
  "repository": "microsoft/aspire",
  "generatedAt": "2026-08-17T21:03:28Z",
  "inputManifest": "runs/2026-08-17T210328Z/manifest.json",
  "summary": {},
  "decisions": [],
  "relationships": [],
  "changesSincePreviousRun": [],
  "reportWarnings": []
}
```

Each decision contains:

- `issueNumber` and `issueUrl`.
- `issueKind`: `incident`, `root-cause`, `tracker`, or `transient`.
- `state`.
- `proposedAction`.
- `confidence`: `high`, `medium`, or `low`.
- `summary`.
- `reasoning`.
- `evidence`: typed supporting references to issue events, comments, runs,
  jobs, logs, pull requests, commits, source paths, and ownership rules.
- `contradictoryEvidence`: typed references to facts that conflict with the
  recommendation.
- `missingEvidence`: typed references to incomplete or unavailable facts.
- `nextCondition`.
- `suggestedOwners` with the reason for each suggestion.
- `relatedIssues` with a typed relationship.
- `changedSincePreviousRun` and the prior decision when available.

`evidence` is the only supporting bucket. Required positive roles for
high-risk recommendations must come from current, available records cited
there. `contradictoryEvidence` and `missingEvidence` capture blockers,
conflicts, and gaps; they can reduce confidence or block an action, but they
never satisfy a required positive role.

Each evidence reference has
`{id, kind, role?, roles?, normalizedCause?}`. `id` and `kind` must match the
snapshot. Exactly one of `role` or `roles` may be supplied. The latter is a
nonempty list of unique finite-role values; singular `role` remains backward
compatible. These fields and the optional `normalizedCause` are the agent's
semantic judgments over the collector's factual record when it provides only
raw facts or logs. The finite role set is `canonical-issue`,
`canonical-search-complete`, `current-failing-run`, `deterministic-marker`,
`known-flaky-signature`, `merged-fix`, `newer-failure`,
`no-newer-matching-failure`, `no-recent-matching-failure`,
`normalized-cause`, `normalized-facts`, `obsolete-surface`,
`post-fix-green`, `prior-resolved-episode`, `recurrence`, and `recovery`.

For compatibility with deterministic fixtures, a snapshot payload may contain
`role`. When present, that value is authoritative. A report reference may omit
its role, repeat the same singular role, or provide `roles` equal to exactly
`[payload.role]`; any other role list invalidates the report. This prevents
deterministic blocker roles such as `newer-failure` or `canonical-issue` from
being relabeled or supplemented.

A deterministic snapshot `payload.normalizedCause` is likewise authoritative.
If a report reference supplies a different value, validation fails. Otherwise
the effective normalized cause is the snapshot value when present and the
report-reference value when the collector supplied no deterministic cause.
Any supplied value must be a nonempty string. A multi-role reference has one
effective normalized cause, consumed only by roles whose gates require it.

Evidence identifiers are globally unique. Issue, pull request, and commit
evidence from another repository includes its `owner/repo` identity so
same-number issues or pull requests in different repositories cannot collide.
Repository strings use strict GitHub syntax: owners contain only alphanumeric
characters and hyphens and must begin and end with an alphanumeric character;
repository names contain one or more alphanumeric, dot, underscore, or hyphen
characters.

Relationships use one of:

- `exact-duplicate`
- `probable-duplicate`
- `canonical-tracker`
- `fixed-by`
- `regression-of`
- `supersedes`
- `same-incident`
- `related`

Each relationship may optionally include `targetRepository`. When that field
is absent, the relationship targets the snapshot repository. When the field is
present, it names the exact `owner/repo` that owns the target issue. `null`,
empty, whitespace, `?`, `#`, colon-bearing, extra slash, owner underscore, owner
leading hyphen, owner trailing hyphen, or otherwise malformed values are
invalid; omit the key entirely for snapshot-repository targets. The shepherd matches
canonical issue relationships on the full repository + issue number pair, not
the issue number alone, so evidence from another repository cannot be treated as
local merely because the numbers match.

A relationship may target the same issue number as the source issue only when
it also supplies a valid `targetRepository` that differs from the snapshot
repository. Same-number relationships without `targetRepository`, or with
`targetRepository` equal to the snapshot repository, are self-references and are
invalid.

Only direct primary-source evidence from the current run may support a
high-confidence destructive recommendation. `exact-duplicate`,
`canonical-tracker`, `fixed-by`, `regression-of`, obsolete-surface evidence,
and bounded no-match searches may contribute, but each must be cited
explicitly.

## Lifecycle States and Proposed Actions

Every open issue receives exactly one state:

| State | Meaning |
| --- | --- |
| `observing` | Evidence supports waiting for a defined recurrence or time condition. |
| `actionable` | The failure is sufficiently understood to investigate or fix. |
| `needs-human` | Progress requires a named human or team decision or access. |
| `fix-in-progress` | A verified linked fix is open or not yet deployed. |
| `awaiting-verification` | A fix is merged, but the required post-fix signal is absent. |
| `resolved` | Verified evidence satisfies the closure condition. |
| `stale` | The affected surface is obsolete or superseded, and closure depends on a bounded no-recent-match search. |
| `tracked-elsewhere` | A separate canonical problem issue now owns the recurring defect. |
| `regression` | A new failure episode recurs after a verified resolution. |
| `duplicate` | Another issue is the verified or strongly supported canonical record. |
| `insufficient-evidence` | The missing evidence prevents a safe decision. |

Every decision proposes exactly one action:

- `wait`
- `investigate`
- `fix`
- `open-dedicated-issue`
- `ping-human`
- `merge-duplicate`
- `close-resolved`
- `close-stale`
- `close-as-tracked`
- `close` (compatibility only for legacy prototype reports)
- `open-regression`

The state describes present reality; the action describes the recommended next
operation. For example, `awaiting-verification` normally proposes `wait`,
`resolved` proposes `close-resolved`, `stale` proposes `close-stale`, and
`tracked-elsewhere` proposes `close-as-tracked`. The generic `close` action
remains valid only so previously generated prototype reports can still be
validated.

## Decision Rules

### Incident records and canonical problem issues

`ci/main` issues are incident records. They capture a concrete failure episode,
not the long-term home for a recurring flaky test or recurring infrastructure
defect. When evidence shows the same problem spans multiple incidents, the
shepherd first searches for an existing canonical problem issue.

If the canonical issue exists, the incident transitions to
`tracked-elsewhere` with `close-as-tracked`. If no canonical issue exists, the
incident remains open and the shepherd recommends `open-dedicated-issue` so a
human can create the canonical problem issue explicitly.

### Waiting

A `wait` recommendation must name a bounded condition, such as:

- One additional occurrence of the same normalized failure within seven days.
- The next scheduled run of a specific workflow.
- Merge or deployment of a verified pull request.
- One green run on the affected branch after the fix commit.

“Wait and see” without a condition is invalid.

### Investigation and fixes

`investigate` is used when the issue is actionable but the root cause or
appropriate fix is not established. A failure signature, recurrence count, or
reproducible symptom is enough to support `investigate`. `fix` requires a
specific root cause and a concrete remediation that can be implemented in this
repository.

The prototype may recommend preparing a fix but does not edit code.

`open-dedicated-issue` is reserved for recurring flaky-test or infrastructure
problems that are still active in a current run, have recurrence evidence or a
known flaky signature, and have completed the canonical-issue search without
finding an existing problem issue. It is valid only for `issueKind: incident`;
root-cause, tracker, and transient issues must use other actions.

### Human escalation

`ping-human` must include:

- A suggested owner or team supported by CODEOWNERS, affected-path history, a
  linked change, or workflow ownership.
- A concrete question or requested decision.
- The evidence already gathered.
- Why the agent cannot safely proceed without that input.

General requests such as “please investigate” are not sufficient.

### Duplicate consolidation

`merge-duplicate` requires a canonical issue and an explanation of why it is
canonical. Its `exact-duplicate` relationship must target the same repository
and issue number as the supporting canonical issue evidence. Exact matching may
use deterministic workflow markers or identical normalized failure evidence.
Semantic similarity without corroboration yields at most medium confidence.
Every available current supporting `canonical-issue` record must resolve to the
same repository and issue number. Multiple compact and qualified records for
the same issue are acceptable; any conflicting canonical issue blocks the
recommendation regardless of evidence order.
Required canonical identities still come only from supporting evidence, but a
current `canonical-issue` record in `contradictoryEvidence` or `missingEvidence`
that resolves to a different repository + issue pair also blocks the
recommendation. A cited current canonical record in those buckets that cannot
resolve to a repository + issue pair blocks conservatively instead of allowing
the action to proceed.

An issue that mixes multiple root causes is classified as a tracker or flagged
for splitting; it is not used as a canonical root-cause issue merely because
it is older.

`close-as-tracked` is distinct from `merge-duplicate`. It applies when the
decision issue is an incident whose recurring defect is already owned by a
separate canonical problem issue. The recommendation requires
`canonical-issue` evidence plus either a `canonical-tracker` or
`exact-duplicate` relationship from the decision issue to that canonical
record, and the canonical evidence must match that relationship's repository
identity and issue number. Repository identity comparisons are
case-insensitive, but reports preserve the spelling observed in source
evidence. If `targetRepository` is absent on the
relationship, the target is in the snapshot repository; external canonical
evidence cannot satisfy the relationship by matching the issue number alone.
If both acceptable relationship types are present, they must resolve to the
same repository and issue number before the recommendation is considered safe.
Every available current supporting `canonical-issue` record must resolve to the
same repository and issue number as that relationship target. Multiple compact
and qualified records for the same issue are acceptable; any conflicting
canonical issue blocks the recommendation regardless of evidence order.
Current `canonical-issue` identities in `contradictoryEvidence` or
`missingEvidence` are blockers when they resolve to a different repository +
issue pair, or when the report claims the canonical role but the identity cannot
be resolved.
Canonical evidence for another issue used by either `merge-duplicate` or
`close-as-tracked` must also be associated with the decision issue through
`payload.sourceIssueNumber` or
`payload.referencedBy[*].sourceIssueNumber`. The relationship establishes
identity agreement but does not make otherwise unrelated evidence reusable.

### Resolution

`close-resolved` requires primary-source evidence that the incident's own
closure condition is satisfied. Depending on issue kind, this normally
includes:

- A verified merged fix or a demonstrated infrastructure recovery.
- A relevant green run after the fix or recovery.
- A completed current search that finds no newer matching failure.
- No contradictory newer occurrence of the same cause.

A merged pull request without post-fix verification normally produces
`awaiting-verification`, not `resolved`.

The validator accepts `no-newer-matching-failure` only from available
workflow-run evidence with `recentHistoryCollected: true`, a list-valued
`recentHistory`, a boolean `recentHistoryTruncated`, and
`historyCoversSourceRun: true`. The agent decides whether the bounded history
semantically matches the incident; the validator proves that collection
completed and that the bounded window covers every run newer than the source.
Missing or malformed history, an endpoint error, or an uncovered truncated
window cannot satisfy the role.

`close-stale` requires stronger evidence than age. The affected workflow, test,
or code path must be removed or superseded, and a bounded recent-history
search must find no matching failure.
The `no-recent-matching-failure` role has the same
strong factual collection and source-coverage requirements.

The generic `close` action remains available only to preserve compatibility
with legacy prototype reports that predate the explicit disposition actions.
It is compatibility syntax only: it requires the same merged-fix-or-recovery,
post-fix-green, no-newer-matching-failure, and no-newer-failure gates as
`close-resolved`.

Each effective role is validated independently for identity, availability,
current-source status, issue association, and factual collection proof. This
allows one workflow-run reference to satisfy both `post-fix-green` and
`no-newer-matching-failure` without duplicating it across a bucket.

`post-fix-green` additionally requires deterministic success evidence. A
workflow run qualifies when the source run has `conclusion: success` or its
rigorously covered `recentHistory` contains a successful run. A workflow job
qualifies only when that job has `conclusion: success`. The agent remains
responsible for comparing that success chronologically with merged-fix or
recovery evidence.

### Regression

`open-regression` applies when the same verified root cause recurs after a
prior episode was resolved. The historical issue remains closed. The proposed
new issue links to the prior episode and carries the new run evidence.
The three supporting role references—`current-failing-run`,
`prior-resolved-episode`, and `normalized-cause`—must each have the same
nonempty effective `normalizedCause`. A deterministic snapshot cause wins; a
conflicting report-reference cause invalidates the report; otherwise the
report-reference cause supplies the semantic normalization.
The `regression-of` relationship must match the prior resolved episode's
repository identity and issue number. Repository names compare
case-insensitively. Issue evidence IDs derive that identity directly. Non-issue
prior evidence, such as a workflow run, must carry `priorIssueNumber` and may
carry `priorRepository`; an absent `priorRepository` means the snapshot
repository. All available current `prior-resolved-episode` records must agree
on that identity.
Prior-episode evidence for another issue must additionally be associated with
the decision issue through `payload.sourceIssueNumber` or
`payload.referencedBy[*].sourceIssueNumber`; a `regression-of` relationship
alone does not establish evidence association.
Required prior-episode identity comes only from supporting evidence, but a
current `prior-resolved-episode` record in `contradictoryEvidence` or
`missingEvidence` blocks `open-regression` when it resolves to a different
repository + issue pair, or when the report claims the prior role but the
identity cannot be resolved.

The shepherd never recommends reopening an issue closed as fixed.

## Confidence and Safety

High confidence requires direct and internally consistent primary-source
evidence. Medium confidence allows a semantic inference corroborated by more
than one independent signal. Low confidence means the recommendation is a
lead, not an action candidate.

The following recommendations are considered high risk:

- `close`
- `close-resolved`
- `close-stale`
- `close-as-tracked`
- `open-dedicated-issue`
- `merge-duplicate`
- `open-regression`

They must not be labeled safe unless all required references were fetched from
GitHub during the current run. Cached prose, issue-body claims, and previous
agent conclusions are not sufficient. Required positive roles come only from
supporting `evidence`; `contradictoryEvidence` and `missingEvidence` can block
an action, but they never satisfy one.

Only an effective role on a current, `available`, supporting reference can
satisfy a required positive gate. That evidence must be deterministically
associated with the decision issue through `payload.sourceIssueNumber`,
`payload.referencedBy[*].sourceIssueNumber`, or the decision issue's own compact
or repository-qualified issue evidence record. A role on evidence associated
only with another issue does not satisfy the gate. Previous-report records
never satisfy roles.

Issue comments and timeline events carry their issue's direct
`sourceIssueNumber`. Source-path and CODEOWNERS records inherit sorted,
deduplicated `referencedBy` associations from the local pull-request and commit
records that name each path. Paths from external repositories do not receive
local ownership associations. These records are consequently included by the
same decision-scoped high-risk completeness check as other issue evidence.
Selected supporting issue records likewise merge `referencedBy` associations
from every explicit reference, marker match, and fact match that selected them,
including one association for each open issue when a supporting issue is shared.

Before an action-specific high-risk check runs, the validator performs
decision-scoped completeness validation. It finds every current snapshot
record tied to the decision issue by those same association rules, regardless
of availability and regardless of whether the record has a role. Every such
record must be cited exactly once across `evidence`, `contradictoryEvidence`,
and `missingEvidence`. Previous-report records are excluded, and unrelated
evidence is not scanned. This prevents selective omission without turning the
validator into a global snapshot scan.

The roles consumed by each action gate are:

- `close` and `close-resolved`: `merged-fix`, `recovery`,
  `post-fix-green`, `no-newer-matching-failure`, `newer-failure`.
- `close-stale`: `obsolete-surface`, `no-recent-matching-failure`,
  `newer-failure`.
- `close-as-tracked`: `canonical-issue`.
- `open-dedicated-issue`: `current-failing-run`, `recurrence`,
  `known-flaky-signature`, `canonical-search-complete`, `canonical-issue`.
- `merge-duplicate`: `canonical-issue`, `deterministic-marker`,
  `normalized-facts`.
- `open-regression`: `current-failing-run`, `prior-resolved-episode`,
  `normalized-cause`.

`close` and `close-resolved` need merged-fix or recovery evidence,
`post-fix-green`, and `no-newer-matching-failure`. An available
`newer-failure` in any evidence bucket blocks `close`, `close-resolved`, and
`close-stale`. `close-stale` needs `obsolete-surface` and
`no-recent-matching-failure`.

`close-as-tracked` needs supporting `canonical-issue` evidence plus a
`canonical-tracker` or `exact-duplicate` relationship whose
`targetIssueNumber` and optional `targetRepository` match that evidence.
Repository names are compared case-insensitively and retained as written in the
report. When `targetRepository` is absent, the relationship targets the
snapshot repository; external evidence cannot be treated as local by matching
the number alone. The action is valid only for `issueKind: incident`. Every
available current supporting `canonical-issue` record must resolve to the same
repository and issue number as the selected
relationship target.
Current `canonical-issue` records in `contradictoryEvidence` or
`missingEvidence` block when they establish a different canonical identity or
when the report claims the role but the identity cannot be resolved.

`open-dedicated-issue` needs a supporting `current-failing-run`,
`recurrence` or `known-flaky-signature`, and `canonical-search-complete`. An
available `canonical-issue` in any bucket blocks this recommendation. The
action is valid only for `issueKind: incident`.
`canonical-search-complete` is eligible only on available issue evidence whose
factual `supportingSearch` says `complete: true`, says `truncated: false`, and
contains a list-valued `candidateIssueNumbers`. An empty or missing list alone
does not establish that the bounded search completed.

`merge-duplicate` needs supporting `canonical-issue` evidence, a supporting
deterministic marker or normalized-facts signal, and an `exact-duplicate`
relationship targeting the same repository and issue number as that canonical
evidence. Every available current supporting `canonical-issue` record must
resolve to that same identity.

`open-regression` needs supporting current-failure, prior-resolved-episode,
and normalized-cause evidence whose three nonempty effective
`normalizedCause` values are equal, plus a `regression-of` relationship whose
target matches the prior-resolved-episode evidence identity. Snapshot causes
take precedence over report-reference causes, and conflicts are invalid.
Repository names are compared case-insensitively. Run-based prior evidence must
include `priorIssueNumber` and may include `priorRepository`; absent
`priorRepository` means the snapshot repository.
Current `prior-resolved-episode` records in `contradictoryEvidence` or
`missingEvidence` block when they establish a different prior identity or when
the report claims the role but the identity cannot be resolved.

Expired logs, inaccessible artifacts, rate limits, malformed references, or
contradictory timelines are surfaced in `missingEvidence` or
`contradictoryEvidence`. The corresponding decision becomes
`insufficient-evidence` or has reduced confidence.

## Previous-run Comparison

When a valid previous report exists, the shepherd identifies:

- Newly opened issues.
- Newly closed or disappeared issues.
- New occurrences or timeline transitions.
- Changed state, action, confidence, owner, or canonical relationship.
- Recommendations that became blocked by contradictory evidence.
- Previously missing evidence that is now available.

The chat summary prioritizes changed decisions. Stable `observing` and
`insufficient-evidence` items remain in the detailed report but do not dominate
the summary.

The previous report is context, not evidence. Current factual claims must
still be supported by the current snapshot.

## Human-readable Report

`report.md` contains:

1. Run metadata and collection health.
2. Changed and high-priority recommendations.
3. Safe close candidates.
4. Duplicate and canonicalization candidates.
5. Actionable investigations and fixes.
6. Human escalations with concrete questions.
7. Observing and awaiting-verification items.
8. Insufficient-evidence items and exact collection gaps.
9. Changes since the previous run.
10. Per-issue evidence appendix.

The concise chat summary includes counts and only the most important changed
items. It links to the local Markdown and JSON reports.

## Adaptive Evidence Expansion

The bounded first pass remains the default inventory and triage input. When it
cannot validate a candidate lifecycle action, the shepherd may request a
second, narrowly scoped read-only evidence pass instead of accepting
`insufficient-evidence` or increasing every global collection limit.

The shepherd writes `evidence-requests.round-N.json`. Requests are declarative and
allowlisted; the agent never supplies a GitHub endpoint or free-form API query.
Each request names an open source issue, explains which decision gate it may
unblock, and references factual evidence already present in the current
snapshot.

Supported request types are:

- `issue-reference`: enrich one partial or `not-enriched` issue or pull-request
  reference already associated with the source issue.
- `workflow-run`: enrich one partial or `not-enriched` run already associated
  with the source issue, including the existing bounded failed-job/log profile
  and one covered first-page history request.
- `canonical-search`: search repository issues using an exact extracted fact
  from the source issue, such as a test name, exception type, error code,
  workflow, job, or step. The collector constructs the query; the agent cannot
  supply arbitrary search text. When a result collides with baseline evidence,
  preserve every baseline `referencedBy` association and merge the
  request-derived association without replacing or duplicating an existing
  source association.
- `source-check`: inspect one evidence-backed affected path in the supplied
  checkout to determine whether the surface still exists or was superseded.
  Every write, including partial and error results, preserves all
  `referencedBy` associations from a colliding baseline source record and
  merges the request-derived associations.

The expansion validator rejects requests for unknown source issues, unscoped
evidence, unsupported request types, arbitrary repositories, invented fact
values, duplicate requests, or evidence that is already fully available.
Expansion remains GET-only and writes a new immutable snapshot rather than
altering the baseline input.

One run may perform at most two expansion rounds, 25 requests per round, 10
canonical searches per round, and five requests for one source issue per
round. Search and history requests use one page with at most 20 and 10 results
respectively. Existing GitHub-client retry behavior still applies, so these are
result and endpoint-family budgets rather than a total HTTP-request guarantee.

Every expansion round writes:

```text
evidence-requests.round-N.json
input.round-N.json
expansion-errors.round-N.json
api-calls.jsonl
```

The agent reassesses all issues against the newest snapshot after each round.
It stops early when remaining requests cannot change a proposed action, and it
must stop after round two. Failure or truncation remains explicit evidence;
the agent may not treat an attempted request as a completed search.

For example, a first pass may identify #19149 as a possible
`close-resolved` candidate but omit its linked fix and failed run due global
budgets. The adaptive pass may fetch the already referenced fixing pull
request and run history. A flaky incident may request a canonical search using
its extracted test name before recommending either `open-dedicated-issue` or
`close-as-tracked`.

## Error Handling

Collection is best effort per resource, not all-or-nothing. A failure to fetch
one run log does not discard successfully collected issue timelines. Each
error records:

- Resource and operation.
- HTTP or `gh` error category.
- Whether the error is retryable.
- Attempts made.
- Effect on downstream decisions.

The collector retries rate limits and transient server failures using bounded
backoff. Authentication, authorization, schema, and invalid-reference errors
are not hidden by retries.

If the primary open-issue inventory cannot be completed, the run fails and
does not update `latest.json`. If secondary evidence is partial, the report is
generated with explicit warnings and affected decisions are constrained.

Invalid shepherd output fails schema validation and is preserved for
diagnosis, but it does not become the latest successful report.

## Testing and Evaluation

### Collector tests

Fixture-based tests cover:

- Pagination across issue, comment, timeline, run, and job APIs.
- Union and deduplication of the two target labels.
- Closed-issue lookback and explicit old references.
- Deterministic, merged supporting-issue associations for explicit,
  marker-match, and fact-match selection.
- Timeline episode normalization.
- Reference extraction from issue bodies and comments.
- Run, pull request, and commit verification.
- Expired artifacts and unavailable logs.
- Rate limits, partial API failures, and fatal inventory failures.
- Stable output ordering and normalization.
- Atomic latest-run updates.
- Single-page history collection, malformed/error handling, and source coverage
  for truncated source-in-window and complete short-window cases.

Each test names the failure it detects. For example, the pagination test must
fail if an implementation silently drops the second page of open issues.

### Shepherd corpus

An anonymization step is not required because evaluation runs locally against
public repository data. The expected-decision fixture records only issue
numbers, evidence references, and the material classification expected from
the audited corpus.

Initial acceptance cases include:

- #19166 is recognized as having a merged fix in #19175 and a backport in
  #19186; closure still depends on the required verification signal.
- #18755 and #18794 are associated with merged fix #18798.
- #19143 is identified as a duplicate of #18840.
- #19379 is identified as duplicating the failure represented by #19363.
- #18657 is recognized as mixing unrelated Outerloop test failures rather
  than representing one root cause.
- #18629 is escalated as a long-running unattended Deployment E2E failure.
- #18608 and #18880 are interpreted using their close/reopen episodes rather
  than assuming every recurrence belongs in one perpetual issue.
- #18897 is recognized as superseded by actionable issue #18898, with the
  relevant test guard treated as supporting evidence rather than proof that
  the underlying cause is fixed.

Expected decisions distinguish mandatory conclusions from judgment ranges.
For example, an issue may permit either `awaiting-verification` or
`insufficient-evidence` depending on current log availability, but it must not
be reported `resolved` without a verified post-fix green run.

### Report validation

Tests validate required fields, evidence-reference integrity, lifecycle and
action enums, relationship constraints, optional report-reference normalized
causes, snapshot-cause conflict handling, strong history proof, and
deterministic section ordering. A collector-shaped regression case verifies
that report references can supply matching normalized causes when raw
collector evidence has none.

A regression in a decision rule must cause at least one named corpus case to
fail with an explanation of the changed classification.

## Rollout

### Phase 1: Manual read-only runs

Run the skill manually, inspect reports, and compare recommendations with the
audited corpus. No GitHub writes are possible.

### Phase 2: Report tuning

Tune normalization, evidence requirements, classifications, and report
prioritization. Add newly discovered representative cases to the evaluation
fixture.

### Phase 3: Local scheduling

After report output stabilizes, schedule the same read-only skill locally.
Scheduling does not change the collector or report contracts.

### Phase 4: Gated write actions

Introduce user-approved write capabilities one action category at a time.
Each category requires a separate design update, dry-run comparison, audit
log, and explicit safety threshold.

### Phase 5: Workflow lifecycle improvements

Refactor repository workflows so deterministic fingerprints, bounded incident
episodes, exact deduplication, and objective close-on-green behavior occur at
filing time. The shepherd continues to handle semantic diagnosis,
cross-system consolidation, ownership, escalation, and remediation judgment.

## Independent Plan Review Gate

After the implementation plan is complete and before implementation begins, a
separate general-purpose agent using GPT-5.6 Sol with high reasoning will
review the approved design and full plan.

The review must challenge:

- Scope and decomposition.
- Assumptions about GitHub APIs and local skill behavior.
- Collector and agent boundaries.
- Data schema stability and migration.
- Evidence and safety requirements.
- Error handling and privacy.
- Test falsifiability and corpus coverage.
- Installation, rollback, and operational maintainability.
- Missing failure modes and unnecessary complexity.

Accepted findings are incorporated into the plan. Consequential disagreements
or changes to approved behavior are returned to the user before implementation.
Implementation remains blocked until this review is complete.

## Success Criteria

The prototype is successful when:

- It inventories every open issue carrying either target label without
  duplicates.
- Every issue has one valid state, one valid proposed action, and cited
  evidence or an explicit evidence gap.
- The named acceptance corpus meets its mandatory expected decisions.
- Repeated runs with unchanged fixtures produce equivalent normalized JSON.
- Partial evidence cannot produce a high-confidence destructive
  recommendation.
- The latest-run pointer cannot reference an incomplete or invalid run.
- A reviewer can identify changed, actionable, and blocked issues from the
  first screen of the Markdown report.
- The system performs no GitHub writes and requires no write-capable token.
