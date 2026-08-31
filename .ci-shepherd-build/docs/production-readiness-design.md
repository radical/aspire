# CI Shepherd Production-Readiness Design

This document describes the design required before the CI shepherd is allowed to
make GitHub-visible changes to `microsoft/aspire`.

It covers deterministic provenance, source resolution, eligibility,
authorization, execution preflight, and post-action reconciliation. It also
defines the flaky-evidence model that gates quarantine.

## How to read this document

Every section is split into two clearly marked parts.

**Implemented today** describes behavior that exists in
`.ci-shepherd-build/scripts/` right now and can be verified by reading the code
or the recorded run artifacts.

**Proposed** describes behavior that does not exist yet. Nothing in a Proposed
block may be assumed to be working, and no operational decision should rely on
it until it has a passing regression test.

Where the two disagree, the Implemented section is authoritative.

## 1. Current state summary

The pipeline collects evidence with GET-only access, prepares a compact handoff,
sends first-seen or materially changed cases to a fresh assessment agent, merges
sparse agent overrides into `judgments.json`, and renders deterministic
proposals.

`action-proposals.json` is the only source of GitHub-visible effects. The
executor supports exactly three operations: issue `create-comment`,
`edit-comment`, and `close-issue`.

Every mutation requires a machine-readable grant that binds the repository, the
absolute state directory, the snapshot ID, a SHA-256 digest of the raw proposal
bytes, explicit action IDs, operations, issue targets, chain roots, an expiry,
mutation and chain budgets, and the production-comment-pilot capability.

Production remains denied by default at three independent boundaries. The
action grant generator and loader accept `microsoft/aspire` only when their
callers explicitly enable the production comment pilot. The exact grant then
records that capability and is constrained to one dependency-free
`edit-comment` action against an existing shepherd-owned comment, no suppression
override, and a round-one expanded snapshot collected less than 15 minutes
earlier. The grant expires no later than 15 minutes after that collection.
Execution requires the same explicit confirmation. Finally, `github_actor.py`
permits only the fixed issue-comment PATCH endpoint for a separately configured
protected comment repository; closure and every other mutation remain denied.
`quarantine_authorization.py` still hard-denies `microsoft/aspire` without
exception.

Two facts from the recorded production dry run shaped the initial design.

First, all seven proposals in that run were `eligible: false`. That gap is now
closed for the bounded comment pilot. On 2026-08-30, a full cycle produced an
eligible one-action proposal for `radical/aspire#74`; an exact grant drove the
comment through the real executor. Replay returned byte-identical output without
another API call. A competing edit refused with `comment-body-changed`, deletion
produced a new create proposal, stale source timestamps and removed labels
refused before mutation, and a SIGKILL after GitHub accepted the POST reconciled
the fsynced intent without creating a second comment.

The same live gate used `radical/aspire#68` to prove that a non-default
`ping-human` pull-request judgment survives an unchanged incremental cycle and
is discarded after the head SHA changes. The audited actor traffic contained
only the two intended POSTs to `radical/aspire` and no production mutation.

Second, the quarantine proposal contained six candidates, and inspection of the
checkout and the source issues showed that several were not valid quarantine
targets. Candidate quality is currently enforced only by prose inside the
generated `workerPrompt`, which is model-dependent rather than deterministic.

### 1.1 Repository portability boundary

The shepherd must not encode Aspire's CI vocabulary as universal behavior. Its
architecture has three layers:

1. The generic engine owns bounded GitHub reads, evidence records, artifact
   integrity, TRX parsing, deterministic classification, grants, replay
   protection, and reconciliation.
2. A repository policy file owns workflow and artifact conventions, accepted
   evidence classes, labels, budgets, and protected-repository rules.
3. A repository adapter owns source resolution and mutation mechanics. Aspire's
   adapter uses QuarantineTools, test-project builds, MTP discovery, and the
   `QuarantinedTest` attribute; another repository may use different tooling.

**Implemented today.** The supported cycle loads the strict, versioned
`policies/repositories/aspire-v1.json` file. Retry-result aggregation job
suffixes, artifact names, trusted workflow events, head-repository rules, TRX
path identity, and test-job identity come from that file rather than the
collector. The policy also owns the quarantine pull request's exact base ref,
allowed head repositories, and required approval count. The canonical policy
document and its SHA-256 digest are embedded in the snapshot and lifecycle
events, and validation rejects content changes or repository mismatches. A
second checked-in test profile uses different retry conventions and drives the
same collector behavior.

**Proposed.** Issue labels, evidence thresholds, mutation budgets,
protected-repository capabilities, worker text, and quarantine adapter
selection remain to be moved behind the same boundary. Until those fields are
explicit and grant-bound, the repository profile is intentionally incomplete
and must not be presented as general multi-repository support.

Repository policy is trusted executable input even when represented as data. A
policy change can alter which evidence or mutation is accepted, so grants must
bind the canonical policy digest. The target repository's current checkout
must never be allowed to substitute a different policy after authorization.

## 2. Provenance and source resolution

### 2.1 The required chain

A candidate is only actionable if an unbroken, machine-checkable chain exists:

```text
issue
  -> occurrence row
    -> workflow run + attempt
      -> failed job + test-result artifact
        -> exact canonical test method name
          -> source method at the checkout commit
```

Each link must be represented by a concrete identifier, not an inference. A
missing link is a hard stop, not a lower confidence score.

### 2.2 Implemented today

Occurrence rows are parsed from issue bodies and enriched from workflow runs.
Proposals carry `evidenceIds` such as `issue:19742` and `run:33086828076`, and a
`sourceEvidenceFingerprint` used to detect material change.

`fingerprints.jsonl` is an append-only exact-fingerprint occurrence ledger that
survives scratch cleanup, so recurrence counts are stable across runs.

Eligibility already records `unavailableEvidenceIds` when a cited run or job log
cannot be fetched, and `untrustedReferenceEvidenceIds` when a reference rests
only on a bare `#1234` mention.

There is currently no verification that a cited run actually contains a job that
executed the named test, and no resolution of a test name to a source method in
the checkout.

### 2.3 Proposed: structured test identity from existing tooling

The repository already resolves failing tests from test-result artifacts.
`tools/CreateFailingTestIssue` downloads run artifacts, extracts failed tests,
and emits JSON containing a canonical test name per failure, falling back to
failed job logs when artifacts are unavailable. It also records which jobs
matched each test.

The shepherd should consume that structured identity rather than re-deriving
names from issue titles or log text.

Rules:

- When a structured artifact-derived canonical name is available for an
  occurrence, it is the only accepted test identity for that occurrence.
- When only a log-derived name is available, the occurrence is marked
  `identitySource: log-derived` and cannot by itself support quarantine.
- When only the issue title is available, the occurrence is marked
  `identitySource: title-derived` and is never a quarantine identity.

Recording `identitySource` on every occurrence is what makes the evidence model
in section 4 auditable rather than a heuristic.

### 2.4 Proposed: run and job binding

Every `run:` evidence ID attached to a candidate must resolve to a run that
contains at least one failed job whose extracted failed-test set includes the
exact canonical name.

Evidence rows that fail this join are dropped from the candidate and reported,
not silently retained. If fewer than the required number of rows survive, the
candidate is excluded with reason `insufficient-recurrence`.

This directly addresses recurrence rows attributed to unrelated jobs or runs,
including cases where an unrelated issue reference was absorbed into a cluster.

### 2.5 Implemented: exact source resolution via QuarantineTools

`tools/QuarantineTools` already uses Roslyn (`Microsoft.CodeAnalysis.CSharp`) to
locate and edit test methods. It returns 3 when no method matches and returns 0
with no modified file when the matching method already carries the requested
attribute. These behaviors are defined in
`tools/QuarantineTools/Quarantine.cs`.

The source gate adds a read-only inspection mode to the existing tool and invokes
it once with every candidate in the cycle:

```bash
dotnet run --project tools/QuarantineTools -- \
  --inspect \
  "Aspire.Dashboard.Tests.Model.DashboardClientTests.SubscribeResources_Foo"
```

The JSON response contains one exact result per requested method. A method is
eligible only when it resolves once and has neither `QuarantinedTest` nor
`ActiveIssue`. Missing, ambiguous, already-quarantined, and already-disabled
methods are reported with distinct exclusion reasons. The request records the
checkout commit and a deterministic SHA-256 digest of the exact C# source tree
the inspector scans, the QuarantineTools project, and its repository build
inputs. The commit and digest are read both before and after inspection and must
remain unchanged. The grant therefore binds the resolution to a stable inspected
source, inspector implementation, and build configuration without depending on
local Git diff settings.

Using QuarantineTools' Roslyn name parsing and namespace/type matching avoids a
second resolver drifting from the mutating tool. Inspection is batched into one
process because results remain separate and deterministic; mutation remains one
invocation per candidate to preserve exact issue-URL attribution.

**Fail-closed behavior.** If inspection cannot run for any reason, including a
build failure, a missing SDK, a timeout, a non-zero exit, or output that fails
schema validation, every affected candidate is excluded with reason
`source-inspection-unavailable`. Inspection failure never degrades to "assume
the test exists". Quarantine removes coverage, so the safe default is to do
nothing.

The gate intentionally has no cache. One process scans all candidates, so cycle
cost stays bounded without introducing stale cache state. Any future cache must
include the checkout tree and tool identity, not only the HEAD SHA and method
name.

## 3. Eligibility

### 3.1 Implemented today

`action-proposals.json` carries a document-level `executionEligibility` and a
per-action `executionEligibility` with `eligible`, `blockingReasons`, `ciLabels`,
`collectionComplete`, `occurrenceCount`, `unavailableEvidenceIds`, and
`untrustedReferenceEvidenceIds`.

Observed blocking reasons include `missing-ci-label`, `no-parsed-occurrences`,
`incomplete-collection`, `unavailable-evidence`, and
`untrusted-reference-provenance`.

An unscoped collection error blocks every action. An issue-scoped error blocks
only proposals for the named issues, leaving the document
`partially-eligible` so unaffected actions remain previewable.

`EXECUTABLE_CI_LABELS` is `{automation-broken, ci-failure-cause, test-failure}`
and is currently defined in both `actions.py` and `actor.py`.

### 3.2 Proposed additions

Add a quarantine-specific eligibility block, described in section 4, evaluated
only for quarantine candidates.

Add a regression test asserting the two `EXECUTABLE_CI_LABELS` definitions are
equal, so the proposer and the executor cannot drift apart.

## 4. Flaky-evidence model

This section replaces any notion of a separate-days threshold. Failures do not
need to occur on different days. What matters is whether the evidence
distinguishes nondeterminism from a deterministic defect.

Production collection now records exact failed and passed test names from the
`All-TestResults` artifacts uploaded by the test workflow. It joins each TRX
`UnitTestResult.testId` to `UnitTest.id`, then uses
`TestMethod.className + "." + TestMethod.name` as the canonical method:

```xml
<UnitTestResult testId="..." outcome="Failed" />
<UnitTest id="...">
  <TestMethod className="Namespace.Type" name="Test" />
</UnitTest>
```

This gives Class A direct per-test evidence. Green builds and green lanes remain
indirect evidence and cannot substitute for a structured passing result.

### 4.1 Evidence classes

**Class A — same commit, same lane, retry recovered.**
The same canonical test fails in one attempt and the same lane succeeds in a
later attempt of the same run, at the same commit, with the same matrix
coordinates.

This is the strongest available signal. The commit, the code, and the
configuration are identical by construction, and the exact passing result proves
that the recovery job selected and ran the same test. Class A requires:

- an exact failed method outcome from a failed attempt's TRX artifact;
- an exact passed method outcome from a later successful attempt's TRX artifact;
- the same run ID and 40-character head SHA;
- the same workflow, raw job name, normalized lane, and operating system.

A successful retry lane without the exact passing result is not Class A. It
is blocked with a reason that distinguishes missing per-test proof from a
missing or non-equivalent retry.

**Class B — exact test failed, later equivalent lane succeeded, relevant paths
unchanged.**
The exact canonical test failed in an earlier build, a later build of an
equivalent lane succeeded, and no path in the candidate's relevant-path set
changed between the two commits.

Class B is meaningful evidence of nondeterminism, and it is enough to classify
the issue as a likely flaky candidate. **It does not independently authorize
automatic quarantine.** The commits differ, so the "it passed later" half of the
signal is doubly indirect: it is inferred from lane success rather than from an
observed pass, and it is observed at a different commit.

Automatic quarantine on Class B requires one additional corroborating signal
from this closed list:

- another independent failure-and-recovery episode for the same test, of class
  A, B, or C;
- a green equivalent build **before** the failure, combined with a deterministic
  match against the enumerated known-nondeterministic failure-signature corpus;
- direct retry recovery, which is Class A and subsumes this.

Without corroboration, Class B routes to `investigate` and the report states
which corroborating signal is missing.

**Class C — isolated failure between green equivalent builds.**
A single exact-test failure with green equivalent builds on both sides and
unchanged relevant paths.

Class C is real evidence and the issue should be recognized and reported as a
likely flaky candidate. It is the most indirect form, because both green
observations are lane-level inferences at different commits, so it also requires
corroboration before automatic quarantine. The acceptable corroborating signals
are the same closed list as Class B: recurrence of another independent A, B, or
C episode, or a deterministic known-nondeterministic signature match.

Classes B and C differ in strength, not in permission. Neither is sufficient
alone; both become sufficient with one corroborating signal.

**Initial automation scope.** Only Class A can authorize an automatic quarantine
in the first production-capable version. Classes B and C remain useful report
signals and may route to investigation, but their relevant-path mapping,
corroborating signature corpus, and automatic-quarantine policy are deferred
until Class A has completed a fork quarantine end to end. The detailed B/C model
is retained here as future design, not as an initial implementation requirement.

**Class D — insufficient.**
Title-derived names, shard or job labels, run-scoped ledger guesses, unavailable
logs, evidence from a job that did not run the test, or any name that does not
resolve to exactly one source method at the checkout commit.

Class D never contributes weight. It is not a weak signal to be summed; it is an
absence of evidence, and a candidate composed only of Class D evidence is
excluded.

### 4.2 Critique of the hierarchy

The hierarchy is sound in ordering, but three parts carry most of the risk and
need to be specified rather than assumed.

**"Relevant paths unchanged" is the load-bearing term in B and C.** It requires a
concrete mapping from a test to the paths whose change could plausibly alter its
outcome. A naive mapping such as "the test's own file" is too narrow and will
call real regressions flaky. A mapping such as "the whole repository" makes B and
C unreachable. The proposed mapping is the union of the test's project directory,
the project's transitive first-party project references, and any CI
configuration that affects the lane. This is computable from the project graph
the repository already uses for test selection. Until it is computed
deterministically, B and C must not be used.

**"Equivalent lane" needs a precise definition.** OS, architecture, target
framework, shard index, and any configuration matrix dimension must match. A
"later build succeeded" comparison across different matrix coordinates is not
evidence about the same execution environment, and treating it as such is how an
environment-specific product bug gets classified as flaky.

**Class A depends on attempt-level artifact retention.** If run attempts or
their `All-TestResults` artifacts are not retained long enough, Class A evidence may simply be
unavailable for older issues, biasing the system toward the weaker classes
exactly where confidence matters most. The model must therefore record why a
candidate reached its class, so a review can distinguish "no retry evidence
exists" from "retry evidence exists and disagrees".

One more correction to a tempting shortcut. A rule of the form "a stack frame in
product code means never quarantine" is wrong in both directions. Almost every
test failure has product frames in its stack, so the rule would block nearly all
quarantines. Meanwhile a genuinely deterministic bug can surface with a stack
that appears to be entirely in test or framework code. Stack composition is not
the discriminator. Reproducibility across attempts and lanes is.

### 4.3 Actual-bug protection

Independent of accumulated flaky weight, a candidate routes to `investigate` or
to a blocking-build disposition, never to quarantine, when any of the following
holds. These are hard vetoes, evaluated after evidence classification, and they
override it.

- **Persistence across same-code executions.** The test fails again on a retry
  of the same commit, or fails on every equivalent lane in which it ran, with no
  observed recovery.
- **Deterministic local reproduction.** The failure reproduces on demand at the
  same commit.
- **A relevant source or configuration change that plausibly introduced the
  failure**, with no subsequent recovery at the same code state. A relevant-path
  change followed by a same-code recovery is not a veto; that combination is
  still nondeterminism.

An identical assertion or error signature across occurrences is **not** a hard
veto. Races, ordering bugs, timeouts, and resource contention very often fail
with exactly the same assertion message and the same expected and actual values
every time, because the failing branch is the same branch. Treating a stable
signature as proof of determinism would misroute the most common real flakes.

A repeated identical signature is instead a **risk flag**. It raises the
required evidence: a candidate with a stable signature must reach Class A, or
Class B or C with corroboration, and it must be reported with the flag visible
so a human reviewing the batch sees it. It never alone blocks quarantine, and it
never alone permits it.

### 4.4 Restoring CI health promptly

The purpose of the model is not to make quarantine hard. It is to make
quarantine correct, so that the cases which are genuinely flaky can move quickly.

A Class A candidate whose successful retry is proven to have run the same test
project and selection, whose source method resolves cleanly, and which trips no
veto should be proposable in the cycle in which the evidence appears. No waiting
period is required, because the evidence is as close to conclusive as this
system can get.

Classes B and C never authorize quarantine on their own, regardless of how many
times a single episode is re-observed across cycles. Each requires one
corroborating signal from the closed list in section 4.1. The distinction
between B and C is how much the surrounding evidence already leans toward
nondeterminism, not whether corroboration is needed.

Both B and C are still reported as likely flaky candidates when they fall short.
Recognizing a flake and automatically suppressing it are different actions, and
the report should make the first one available to a human even when the second
is withheld. Everything else waits for better evidence, and the report states
exactly what is missing so a human can supply it.

### 4.5 Candidate exclusion reasons

The proposed quarantine gate emits a closed set of reasons. Every excluded
candidate appears in `blockedTargets` with its reason and is rendered in
`report.md`. Silent exclusion is a defect.

| Reason | Meaning |
| --- | --- |
| `already-quarantined` | Source method already carries the quarantine attribute |
| `already-quarantined-by-label` | Source issue already carries `quarantined-test` |
| `source-labels-unavailable` | The source issue event or its labels are missing or malformed |
| `already-suppressed` | Source method already carries `[ActiveIssue]`, so it is already not running |
| `not-a-test-method` | Target is a shard, job, or otherwise not a method identity |
| `target-not-found-in-checkout` | Well-formed name resolves to no source method |
| `ambiguous-target` | Name resolves to more than one source method |
| `source-inspection-unavailable` | Inspection could not run; fail closed |
| `infrastructure-failure` | Failure signature matched the enumerated infrastructure corpus |
| `suspected-product-defect` | Hard veto in section 4.3 tripped |
| `insufficient-recurrence` | Surviving bound evidence rows below threshold |
| `insufficient-evidence-class` | Highest class reached does not authorize quarantine |

#### Attribute semantics: suppression versus environment gating

Test attributes discovered by inspection must not be lumped together as
"environment-related". They mean different things and lead to different
outcomes.

**`[QuarantinedTest]` and `[ActiveIssue]` mean the target is already
suppressed.** The test is not contributing failures to the signal being acted
on, so applying another quarantine is redundant at best and misleading at worst.
These block the candidate, with reasons `already-quarantined` and
`already-suppressed` respectively. They are blocked because the target is
already handled, not because the failure was infrastructural.

**`[RequiresFeature]` is not evidence of anything.** It is a capability gate
declaring that the test needs a runtime feature such as a container runtime. A
test carrying it can be genuinely flaky, genuinely broken, or perfectly healthy.
Its presence must not, on its own, classify a failure as infrastructural, and it
must not block quarantine.

`infrastructure-failure` is therefore assigned from **failure evidence** — a
match against the enumerated infrastructure failure-signature corpus, such as
runner loss, image pull failure, disk exhaustion, or agent disconnect — never
from the presence of an attribute on the test. A `[RequiresFeature]` test whose
failure signature is a genuine assertion failure is a normal candidate and is
evaluated by the ordinary evidence model.

Inspection still reports `requiresFeature` in its output, because it is useful
context in the report and can raise reviewer attention. Reporting it and gating
on it are different things.

### 4.6 Implemented today

`build_quarantine_session_request` clusters `review-quarantine` recommendations
by `target.value`. It validates method-name shape, source labels, and
deterministic Class A evidence before source inspection. Class A is awarded only
when an artifact-derived exact failure and exact pass share the required retry
identity. The request records the failure occurrence, recovery coverage row,
reason, and complete evidence-ID set.

`cycle.py` persists those deterministic inputs in
`quarantine-evidence.json`. Source inspection then resolves each surviving name
to exactly one method and records its semantic baseline before a proposal is
grantable. Authorization independently requires Class A, checks that the
failure and recovery identify the same run, commit, workflow, job, lane, and OS
with a later attempt, and requires both corresponding test-results evidence IDs.

Classes B and C, infrastructure-signature classification, product-defect vetoes,
and recurrence quality remain deferred. They cannot authorize quarantine in the
current implementation.

## 5. Performance and cost

The gating work added by this design is small and bounded, because it runs on a
tiny subset of the inventory.

**Runs every cycle, unchanged.** Inventory collection, evidence refresh,
fingerprint recording, review selection, and proposal rendering. This is the
dominant cost today and this design does not add to it.

**Runs every cycle, new and bounded.** Attempt-one runs add no retry requests.
For rerun references, collection fetches at most the two preceding attempts,
then downloads at most three `All-TestResults` artifacts. Downloads are capped
at 25 MB and verified against GitHub's SHA-256 artifact digest. ZIP entry count,
TRX count, per-file size, total uncompressed TRX size, and XML declarations are
bounded before parsing. Class A computation itself is local and deterministic.

**Runs only for quarantine candidates.** Source resolution through the
existing QuarantineTools mutation path in a disposable clean checkout. In the
recorded production run there were six candidates against seventy-two reviewed
issues, so this is a single-digit number of per-candidate invocations per cycle.
The inspector and mutation executor run older target-framework tools with the
available .NET 10 runtime by setting `DOTNET_ROLL_FORWARD=Major`; without that
explicit contract, a .NET 10-only checkout can misreport source inspection as
unavailable after successfully building a .NET 8 tool.

**Not cached.** Live GitHub state used for preflight. Caching preflight would
defeat its purpose.

The first implementation pays the process and Roslyn parse cost once per
candidate. Measure it before adding a new inspection protocol, batching, or
caching; never weaken the fail-closed rule to reduce latency.

## 6. Constraining and revalidating agent judgments

### 6.1 Implemented today

The assessment agent has no GitHub access and no ability to execute actions. It
returns sparse overrides, and silence for a selected case means the deterministic
default stands.

`finalize.py` accepts sparse agent changes only for selected cases, carries
forward validated overrides for unchanged omitted cases, and restores
deterministic defaults for the remainder. `judgments.json` is the only validated
decision authority.

The actor never reinterprets `judgments.json`, issue prose, or evidence, and
never regenerates comment text or close reasons.

A `review-close` judgment without deterministic resolution or duplicate evidence
is preserved in `blockedRecommendations` rather than aborting the whole document.

Pull-request judgments follow the same sparse-override model, and the current
code intentionally retains only judgments that differ from the prior
deterministic default.

The recorded replay of an unchanged cycle showed `judgments.json` byte-stable
while `pull-request-judgments.json` went from four entries to empty. That is
**not** evidence of a defect. All four of those entries were `no-action`, which
is the deterministic default, so dropping them is the documented and intended
behavior rather than a loss of agent intent. Nothing in the recorded artifacts
demonstrates that a non-default override fails to carry forward, and earlier
regression tests already cover `ping-human` carry-forward on the pull-request
path.

Two real concerns remain, and they are narrower than "judgments are lost".

First, **reporting continuity for default-reviewed pull requests**. When a
default judgment is correctly not retained, the pull request can disappear from
the cycle's visible coverage even though it was reviewed. The report should
still show that it was reviewed and defaulted, so an operator cannot mistake
"reviewed, nothing to do" for "never looked at".

Second, **live confirmation of non-default retention**. Retention is covered by
local regression tests; it has not been confirmed against a real repository
across a real replay. That is a fork E2E confirmation item, not a suspected
defect.

### 6.2 Proposed: revalidation after merge

Merging agent overrides into `judgments.json` is not the end of validation. The
merged document must be revalidated as a whole, because an override can be
individually well-formed and still produce an invalid combined state.

After merge and before proposal rendering, assert:

- every judgment references an issue present in the current snapshot;
- every `evidenceId` resolves inside the snapshot or an explicitly trusted
  reference set;
- no judgment cites a shepherd-authored status comment as evidence;
- every `review-quarantine` target passes the section 4 gate;
- every `review-close` has deterministic resolution or duplicate evidence;
- dispositions are drawn from the allowed set for their case kind, and closure
  remains unrepresentable for pull requests;
- carried-forward overrides still match the evidence fingerprint they were
  validated against, for issues and pull requests alike.

The last item is the one that matters most in steady state. A carried-forward
judgment is a judgment made against evidence that may no longer exist. Carrying
it forward without rechecking its fingerprint means the system can act on a
conclusion whose basis has changed.

Pull-request judgment handling should keep a regression test asserting that a
non-default override survives an unchanged replay, and should additionally
ensure that a defaulted pull request remains visible in the report even though
its judgment is intentionally not retained.

## 7. Mutation preflight and post-action reconciliation

### 7.1 Implemented today

Execute mode accepts only proposal schema v2 and requires one exact action ID,
one grant, and the grant-bound state directory.

Before any mutation it validates the **frozen** eligibility recorded in the
proposal document. `_validate_execution_eligibility` asserts that the action's
`executionEligibility` object contains exactly the supported fields and that
`eligible` is true, and `_validate_document_execution_eligibility` asserts the
document is not blocked. These are checks on values written at proposal time.

It is important to be precise about what that does and does not mean. The
proposal-time CI label set, occurrence count, scoped collection-completeness
flag, and evidence-availability flag are read back from the proposal document.
The actor independently casefolds the target's current labels and requires at
least one executable CI label, but it does not recollect underlying run or job
evidence. The current `sourceEvidenceFingerprint.issueUpdatedAt` is compared
with the live issue `updated_at`, so normal issue activity makes the proposal
stale.

What execute mode does check live, by refetching the target, is: that the target
still exists; that its kind still matches (issue versus pull request); that its
URL is unchanged; that a pull request is not assigned to Copilot; the current
issue or pull-request state and `updated_at`; and that the issue still carries
an executable CI label. For comment operations it also checks comment existence
and authorship ownership. An `edit-comment` proposal binds a SHA-256 digest of
the source comment body and refuses if the live body changed, preventing an
approved update from overwriting a concurrent edit.

It then fsyncs an `intent` event under a bounded lock, checks recorded
dependencies and the refetched target state, performs one fixed operation,
refetches the target again, and appends a terminal event to
`action-events.jsonl`.

This narrows but does not close the gap that motivates section 7.2. A removed
label is detected. If an occurrence is withdrawn, a collection becomes
incomplete, or evidence becomes unavailable without changing the issue
fingerprint, the current path does not notice. Live evidence revalidation is
therefore Proposed, not Implemented.

A surviving `intent` or `indeterminate` event permits reconciliation only and
never permits another mutation. Reconciliation requires the exact idempotency
key, body, and authenticated author. Interrupted close reconciliation also
requires both the live authenticated login and `closed_by.login` to equal the
proposal's shepherd identity, so a close by another maintainer is not credited
to the shepherd. A human close performed through the same GitHub account remains
indistinguishable; closure should stay disabled until that residual risk is
accepted or a dedicated bot identity is used.

Budget consumption is derived from the grant ID in the grant-bound append-only
event log, so copying the grant file does not reset the budget.

A dependent close runs only after its comment reconciles successfully.

### 7.2 Proposed: explicit preflight contract

Preflight should be a single named check with an explicit, enumerated
precondition list, evaluated immediately before the API call and recorded in the
`intent` event. Recording the preconditions in the intent event is what makes a
crash reconcilable, because the reconciler can compare what was expected against
what is now live.

For `create-comment`:

- issue exists, is in `expectedIssueState`, and is not assigned to Copilot;
- issue still carries a label in `EXECUTABLE_CI_LABELS`;
- no existing comment carries the same idempotency key with an authored-by-self
  marker;
- the frozen body is byte-identical to the approved proposal body and begins
  with `[automated] `;
- the evidence fingerprint is unchanged since proposal generation.

For `edit-comment`, additionally:

- the target comment still exists;
- its author equals the authenticated login, not merely the configured
  `shepherd-author`;
- its current body hash equals the hash recorded at proposal time (implemented).

That last precondition is the one that prevents a silent overwrite of a human
edit. Without it, an edit can succeed, satisfy every other invariant, record a
clean terminal event, and still destroy someone's text.

For `close-issue`, additionally:

- the dependency comment has a terminal success, not merely a terminal event;
- the issue is still open;
- no new human comment has appeared since the evidence fingerprint was taken.

The authenticated-login check deserves emphasis. `--shepherd-author` is
operator-supplied. If it disagrees with the token's login, the shepherd will
treat comments it did not author as its own. Preflight should assert
`shepherd-author == authenticated login` once per execution and refuse
otherwise.

### 7.3 Proposed: post-action reconciliation contract

After every attempted effect, reconcile against intent rather than against the
proposal document.

- Refetch the target and compare live state to the intended state.
- For comments, compare the live body to the frozen body and record the
  resulting comment ID.
- For closures, compare live state and close reason.
- For quarantine, compare the pushed diff against the intended diff: only the
  expected attribute additions and required using directives, and no unrelated
  file changes.
- Write the terminal event with the observed result, then regenerate the report
  from live state.

The report must be rendered from reconciled live state, not from the proposal
document. Otherwise a partially executed grant produces a report describing
effects that did not happen.

Partial execution deserves an explicit outcome. If a grant authorizes two
actions and the second fails, the terminal state is "one applied, one not", and
that must be visible in both the ledger and the report rather than inferred.

### 7.4 Implemented: quarantine post-change validation

A quarantine mutation edits source. Comparing a diff is not enough, because a
diff can look correct and still leave the repository in a state where the test
is not actually quarantined, or where the wrong test was changed. Validation
runs as an ordered gate; the first failure aborts the session and nothing is
pushed.

**Step 1 — inspect the edited tree.** Parse the resulting syntax tree after the
per-candidate QuarantineTools invocation and assert:

- the exact canonical method resolves to exactly one source method;
- it now carries exactly one quarantine attribute, not zero and not two;
- the attribute's issue URL is byte-equal to the original issue URL carried in
  the batch entry, not merely a URL to the same repository;
- no other method in the batch's files gained or lost an attribute.

Comparing the issue URL exactly is what catches a correct-looking edit applied
with the wrong argument, which produces a valid attribute pointing at the wrong
issue and is invisible in a summary diff.

**Step 2 — validate the change syntactically.** Parse the diff and the resulting
syntax trees and assert that the only changes are the quarantine attribute
additions and any `using` directive required by that attribute. No statement, no
expression, no assertion, no signature, no other attribute, and no file outside
the expected set may change. Any edit to test logic is a hard failure, not a
warning.

**Step 3 — build every affected project.** A syntactically valid attribute can
still fail to compile, for example when the required `using` is missing or the
attribute is not accessible from that project. Every project containing a
modified file must build cleanly.

**Step 4 — verify selection actually excludes the test.** Run test discovery
under the quarantined-trait filter used by CI and assert the exact method is
**absent** from the selected set. This is the step that proves the quarantine
achieved its purpose rather than merely adding an annotation.

**Step 5 — verify the test still exists.** Separately, run discovery without the
trait filter and assert the exact method is **present**. Step 4 alone can be
satisfied by deleting or renaming the test, which would silently destroy
coverage while producing a passing exclusion check. Steps 4 and 5 must both hold.

**Step 6 — bind the result to the tree before pushing.** Record a canonical
digest of the validated working-tree diff, revalidate it immediately before
commit, then require the resulting single non-merge commit to have the same
exact files and canonical diff digest. Push and open the pull request only from
that validated commit.

`publish_quarantine.py` is the sole initial-publication boundary. It derives the
branch from `batchId`, resolves the Git remote from the policy-owned allowed
head repository, refuses mismatched existing branches rather than
force-pushing, and re-derives commit validation immediately before an
explicit-SHA push. It snapshots the validated pull-request body before the
first mutation. Push and draft-pull-request creation each use a unique, fsynced
intent/outcome operation ID so a crash is visible and a rerun can reconcile an
exact existing branch or pull request without duplicating it. The independent
`microsoft/aspire` production deny remains in force.

**Step 7 — verify the pull request after creation.** Refetch the draft pull
request and assert its target repository, policy-owned base ref, allowed head
repository, draft state, 40-character head SHA, and complete modified-file list
match the validated commit artifact. The commit artifact already binds that
head to the canonical diff digest. A mismatch records the session `failed`; it
does not record a head SHA the shepherd did not validate.

**Step 8 — verify the merge before recording completion.** A session is recorded
`completed` only after fetching all pull request review pages, confirming the
policy-required number of latest decisive reviewer states are approvals, and
refetching the target branch to confirm the quarantine attribute is present on
the exact method with the exact issue URL. `COMMENTED` and `PENDING` reviews do
not revoke an approval; later `CHANGES_REQUESTED` or `DISMISSED` states do.
Merge is not assumed from pull-request state; it is observed in the merged
tree. Until that observation succeeds, the session remains pending and the
affected tests stay suppressed from new batches.

The `radical/aspire` Class A exercise proved the live base, head, file, and
commit checks. Reconciliation then stopped at the required-approval gate because
the fork owner cannot independently approve its own pull request. A separate
deterministic invocation proved merge-commit source verification, but it was not
an integrated reconciliation completion. The authoritative batch remains
`pull-request-open` and cannot be abandoned after its merged side effect. The
production approval requirement remains one; the complete approval-to-merge
transition must be observed on the first guarded `microsoft/aspire` quarantine
pull request.

**Interrupted sessions fail closed.** The `started` event records the exact
authorization grant ID and expiry. Grant expiry never releases the batch:
after an interruption, the same recorded session can be resumed or reconciled
without consuming a second grant. An operator can release a `started` batch
only with an explicit `abandoned` event that confirms no branch, commit, or
pull request was created. If any remote side effect might exist, the batch
remains active until it is reconciled.

## 8. Development sequencing

Do not write the full scenario suite before implementing. Breadth-first test
writing on an unproven mutation path produces a large body of tests that all
depend on behavior that has never run.

Work vertically. One failing regression test through a public interface, the
minimal implementation that makes it pass, then the next scenario.

The tracer bullet is the first priority, because it is the first time the
mutation path completes anywhere:

1. Write one failing test that drives the public cycle interface against a
   controlled fixture and asserts a single comment is created and recorded.
2. Implement only what that test needs.
3. Add the immediate-rerun assertion: the same grant must refuse a second
   mutation.
4. Add preflight preconditions one at a time, each introduced by a failing test.
5. Only then start the quarantine gate, again one exclusion reason at a time.

For the quarantine gate specifically, the recorded production judgments are the
best available regression corpus, because they already contain the failure
classes the gate must catch. Start from that frozen input, assert the expected
exclusions, and watch the test fail before writing the gate.

Within the quarantine work, order matters. Class A and its exact
failure-and-pass proof are implemented first, followed by one exclusion reason
at a time and post-change validation. Classes B and C remain deferred until the
Class A fork lifecycle is complete. Class A comes first because it is the only
class that authorizes quarantine without corroboration, so it is the shortest
path to a working end-to-end quarantine and therefore the fastest way to prove
the whole chain holds. Post-change validation comes last only in implementation
order; no quarantine may be pushed before it exists.

Every test must assert observable behavior through the public cycle, actor, or
artifact surface. Tests that reach into private helpers will pass while the
system is broken, because the failures this design targets are integration
failures, not unit failures.

## 9. Open questions

- What retention window exists for run attempts and test-result artifacts, and
  how often will Class A evidence be unavailable in practice?
- Which fork-only workflow fixture should make `Final Test Results` run when the
  repository owner is not `microsoft`, without changing production workflow
  behavior?
- What is the authoritative source for the relevant-path mapping, and can it be
  derived from the existing test-selection project graph without duplicating it?
- Who curates the known-nondeterministic failure-signature corpus used as a
  corroborating signal for classes B and C, and what is the bar for adding an
  entry?
- Should the shepherd require a clean checkout outright, refusing to inspect a
  dirty tree, rather than fingerprinting the dirty paths?
- Is a defaulted pull request currently visible in the report as reviewed, or
  does reporting continuity need to be added?
