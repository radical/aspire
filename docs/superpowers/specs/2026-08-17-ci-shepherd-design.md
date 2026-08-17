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
- Query a bounded set of recently closed issues with either label.
- Fetch comments and lifecycle events for candidate issues.
- Extract and verify references to workflow runs, pull requests, commits,
  issues, tests, jobs, steps, and branches.
- Fetch referenced workflow-run metadata, jobs, annotations, and available
  failure logs.
- Fetch referenced pull request and commit state.
- Read CODEOWNERS and relevant repository history when a local checkout is
  available.
- Record collection failures and evidence expiry explicitly.

The collector will use `gh api` and documented GitHub REST endpoints rather
than scraping rendered issue pages. Pagination, rate-limit handling, and
reference normalization remain deterministic.

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
will include:

- Issues with either target label closed within the configured lookback.
- Closed issues explicitly referenced by an open issue, linked run, pull
  request, or commit.
- Closed exact-marker matches found by deterministic workflow metadata.
- Closed issues returned as candidate matches for a normalized failure fact.

The default lookback is 90 days. Explicit references are followed regardless
of age.

### Workflow runs

The collector fetches every run explicitly linked from an included issue and
the bounded recent history needed to evaluate a stated next condition. It does
not download every repository workflow run.

For each run it records:

- Workflow identity, event, branch, commit, attempt, conclusion, and timing.
- Jobs, steps, conclusions, and annotations.
- Available failure logs or a precise unavailability reason.
- Rerun relationships.
- Referenced issue markers and failure records when present.

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
- `evidence`: typed references to issue events, comments, runs, jobs, logs,
  pull requests, commits, source paths, and ownership rules.
- `contradictoryEvidence`.
- `missingEvidence`.
- `nextCondition`.
- `suggestedOwners` with the reason for each suggestion.
- `relatedIssues` with a typed relationship.
- `changedSincePreviousRun` and the prior decision when available.

Relationships use one of:

- `exact-duplicate`
- `probable-duplicate`
- `canonical-tracker`
- `fixed-by`
- `regression-of`
- `supersedes`
- `same-incident`
- `related`

Only `exact-duplicate`, `fixed-by`, and `regression-of` may support a
high-confidence destructive recommendation, and each requires direct primary
source evidence.

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
| `regression` | A new failure episode recurs after a verified resolution. |
| `duplicate` | Another issue is the verified or strongly supported canonical record. |
| `insufficient-evidence` | The missing evidence prevents a safe decision. |

Every decision proposes exactly one action:

- `wait`
- `investigate`
- `fix`
- `ping-human`
- `merge-duplicate`
- `close`
- `open-regression`

The state describes present reality; the action describes the recommended next
operation. For example, `awaiting-verification` normally proposes `wait`, and
`resolved` proposes `close`.

## Decision Rules

### Waiting

A `wait` recommendation must name a bounded condition, such as:

- One additional occurrence of the same normalized failure within seven days.
- The next scheduled run of a specific workflow.
- Merge or deployment of a verified pull request.
- One green run on the affected branch after the fix commit.

“Wait and see” without a condition is invalid.

### Investigation and fixes

`investigate` is used when the issue is actionable but the root cause or
appropriate fix is not established. `fix` requires a specific evidence-backed
failure mechanism and a plausible affected area.

The prototype may recommend preparing a fix but does not edit code.

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
canonical. Exact matching may use deterministic workflow markers or identical
normalized failure evidence. Semantic similarity without corroboration yields
at most medium confidence.

An issue that mixes multiple root causes is classified as a tracker or flagged
for splitting; it is not used as a canonical root-cause issue merely because
it is older.

### Resolution

`close` requires primary-source evidence that the issue's own closure
condition is satisfied. Depending on issue kind, this normally includes:

- A verified merged fix or a demonstrated infrastructure recovery.
- A relevant green run after the fix or recovery.
- No contradictory newer occurrence of the same cause.

A merged pull request without post-fix verification normally produces
`awaiting-verification`, not `resolved`.

### Regression

`open-regression` applies when the same verified root cause recurs after a
prior episode was resolved. The historical issue remains closed. The proposed
new issue links to the prior episode and carries the new run evidence.

The shepherd never recommends reopening an issue closed as fixed.

## Confidence and Safety

High confidence requires direct and internally consistent primary-source
evidence. Medium confidence allows a semantic inference corroborated by more
than one independent signal. Low confidence means the recommendation is a
lead, not an action candidate.

The following recommendations are considered high risk:

- `close`
- `merge-duplicate`
- `open-regression`

They must not be labeled safe unless all required references were fetched from
GitHub during the current run. Cached prose, issue-body claims, and previous
agent conclusions are not sufficient.

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
- Timeline episode normalization.
- Reference extraction from issue bodies and comments.
- Run, pull request, and commit verification.
- Expired artifacts and unavailable logs.
- Rate limits, partial API failures, and fatal inventory failures.
- Stable output ordering and normalization.
- Atomic latest-run updates.

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
action enums, relationship constraints, and deterministic section ordering.

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
