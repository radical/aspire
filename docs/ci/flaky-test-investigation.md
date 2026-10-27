# Issue-driven flaky-test investigations

A maintainer can enroll an open Aspire issue by URL alone. Fresh, bounded
agent phases follow the `fix-flaky-test` skill, discover the test from source,
reproduce it, diagnose matching failures, and verify a source-scoped candidate.
The current production path ends with finite, evidence-backed findings.
**Automated candidate execution and fix-PR publication are blocked** until an
independently validated execution boundary protects the trusted supervisor,
configuration and evidence. Separate directories under one host user do not
provide that boundary. Nothing merges, closes the issue, or removes quarantine.

## Start, resume, cancel

After the workflows are registered on the default branch:

```shell
gh workflow run flaky-test-investigation.yml --repo microsoft/aspire \
  -f issue_url=https://github.com/microsoft/aspire/issues/NUMBER \
  -f operation=start
```

Use `operation=resume` to reconcile an existing investigation, or
`operation=cancel` to cancel only its exact owned run. Enrollment and cancellation
require maintainer permission; the issue author need not be a maintainer.
Repeated enrollment does not reset state or budgets.

The agent continues substantive source/history analysis and feasible local work
until it needs external evidence or verification. It does not yield just to
select a test or ask another model to continue reading. The executable prompt
separates this flaky-task guidance from execution and handoff mechanics; it
does not introduce another task implementation or a generic plugin system.

The controller advances actionable transitions and exits; no model waits for CI.
A substantive handoff includes target selection alongside an explicit
reproduction request, a locally developed candidate, or a terminal finding.
Selection alone is rejected rather than starting a discovery-only phase.
Hosted reproduction is optional and happens only when the investigator requests
it. After accepting the exact owned run's evidence, the controller resumes a
fresh phase with the original artifacts and logs; it does not automatically
repeat clean rounds. Final exact-candidate hosted verification remains mandatory.
A scheduled
controller reconciles checkpoints every 15 minutes. Child completion notifications
also wake reconciliation; the triggering payload is never authority for source,
state, artifact or run identity. The controller does not listen to its own
completion, avoiding a recursive wakeup chain. Token-generated completion
notifications and scheduled wakeups are best-effort, not a delivery guarantee.
Manual resume can advance
completed work before the next scheduled check. Progress does not depend on
completion events from token-dispatched workflows. Scheduled recovery can also
resume a completed worker after an interrupted controller.

```text
issue URL → skill-driven source/log/local investigation
  → requested hosted reproduction → resume investigation (when useful)
  → local candidate → exact hosted verification → final source review → draft PR
  → supported terminal findings (without a verified fix)
```

### Operator walkthrough

1. Start with the original issue URL. The first agent receives the issue and
   dated comments, plus an archive of the frozen base source and fixtures at
   `/tmp/gh-aw/flaky-investigation/source`. It must actually read the staged
   `fix-flaky-test` guidance and cite a relevant excerpt. The wrapper checkout
   is not a substitute for that source; the archive itself has no `.git`.
   A header excerpt or assumed missing tool is not enough: the prompt requires
   test/fixture analysis, available linked historical evidence, actual
   prerequisite probes and bounded local execution when feasible.
   The `flaky-investigation-inputs` artifact retains `context.json`,
   `preparation.json` and any staged historical logs before the model runs.
   Preparation records the host SDK list against the frozen `global.json`,
   likely test-source contents and at most two explicitly linked same-repository
   historical job logs. Missing/denied evidence is explicit. Preparation-host
   SDK discovery does not establish the model sandbox's toolchain or restore
   feasibility; the model must recheck and record its actual bounded attempt.
2. Open the controller's console and step summary. `flaky-investigation` JSON
   lines identify the issue, phase, checkpoint, base/control SHA, selected
   test/project/OSes, and current versus checkpointed child URLs. Worker
   identity includes source SHA, mode and runner/iteration scale. Short
   transition reasons, accepted/rejected counts and terminal publication
   uncertainty remain visible without downloading artifacts.
   Model reasoning is labelled separately from controller observations.
   `waiting` means no checkpoint changed; `terminal-noop` means no work restarted.
3. Follow the exact child URL. Worker console lines show each iteration and
   sanitized test output while it runs, including failure details. JSON
   framing and lossless legacy-command escaping keep test/model text from
   becoming Actions commands. Console
   output is bounded; full original bytes remain in each `output.log` beside
   the original TRX, `result.json`, and source receipt. Console text is not
   independently validated test evidence.
4. To involve another maintainer, share the issue, controller/child URLs,
   checkpoint SHA, and candidate/source SHA. Read `state.json`, the immutable
   `repo-memory-default` proposal artifact, and original worker artifacts
   together; do not substitute a newer branch head or merged TRX. Resume
   reconciles existing work and cancel targets only the owned active run.

Each handoff distinguishes observations, the current hypothesis and outstanding
work in `explanation`, with source/report identities and excerpts in
`evidence_read`. Selection requires `local_precheck`; later proposals can reuse
that bounded field for their actual local work. Checkpoint notes preserve it
for fresh agents. Their prepared context also includes immutable base/candidate
identities and the published candidate files. No extra action or
proposal-version change is needed.

These local records are model-authored evidence, not independently validated
test results. A prerequisite/build failure is not a test pass. Older proposals
remain readable without local-work metadata. Local/log evidence can guide a
candidate without a hosted baseline. It cannot replace exact hosted candidate
verification or final source review, or waive execution-isolation and independent
review admissions. Local work alone cannot authorize a fix PR.

The original issue receives the confirmed terminal finding URL. A terminal
resume is a no-op, and repeated start does not reset budgets. There is no
supported restart-after-terminal or source-identity migration operation yet;
inspect the retained state and coordinate manual remediation rather than
deleting refs or assuming another start begins a new investigation.

## Authority and isolation

The trusted implementation is `.github/scripts/flaky_investigation/`.
`contracts.py` validates discovery, repair scope and budgets; `github.py` owns
REST and immutable Git checkpoints; `evidence.py` accounts for original test
records; `worker.py` validates fixed-workflow activation; `runner.py` retains
original test iterations; `controller.py` advances the state machine and publishes.

The checkpoint is `state.json` on
`ankj/flaky-test-<issue>-state`. Each transition creates a new commit and updates
the ref without force. Concurrent writers cannot replace one another's state.
The checkpoint records immutable source/control revisions, exact owned run IDs
and attempts, proposals, counts, candidate identity, and terminal outcome.
Model repo-memory is proposal transport, **not** orchestration authority.
The Python runtime derives repository authority only from the trusted runner's
`GITHUB_REPOSITORY`; issue URLs, APIs and run ownership must match that exact
repository. This permits fork-only validation wrappers without authorizing
upstream writes from a fork. Production workflow activation remains restricted
to `microsoft/aspire`.

The controller records a dispatch intent before calling the API and captures
`return_run_details.workflow_run_id` from the response. It never finds a run by
looking up the latest run. If a dispatch succeeded but its receipt/checkpoint
was lost, recovery preserves the original immutable intent revision and time
before querying the workflow's runs. A complete response of at most 100 records
is required. Recovery verifies the exact repository and head repository,
workflow, unique owned ref, pinned workflow SHA, dispatch event, attempt one,
creation window, and full `display_title` correlation:

```text
Flaky reproduction {exact issue URL} [{full investigation_revision SHA}]
Flaky investigator {exact issue URL} [{full state_revision SHA}]
```

These are exact string matches, not prefixes or extracted dispatch inputs;
the runs API does not expose those inputs. The reproduction workflow emits
the first title for investigation inputs, while ordinary manual reproduction
retains `Reproduce Flaky Tests`. The compiled investigator emits the second.
Runtime source and configuration must still match the original intent.

One uniquely correlated run restores its exact ID without another dispatch,
budget reservation or model phase. A recovered agent retains the original
proposal revision. Resumes do not refresh child deadlines. Missing runs get
five minutes for visibility before a blocked finding; cancellation and
expired budgets do not wait for that grace period.

Zero matches, duplicate/foreign records, incomplete responses, lookup failures,
or multiple correlated runs never prove that no child is active. The controller
posts at most one finding and retains `cleanup-pending`; independent recovery
continues after the task budget expires. A later unique match after a blocker
is cleanup-only, not permission to accept evidence or restart work.
Ambiguous runs are not automatically cancelled. Every previously observed
exact run ID must be confirmed completed before ambiguous cleanup can finish.
If ownership remains unknowable, restore API access or inspect the immutable
intent and matching runs manually; absence alone cannot terminate recovery.
Already-terminal checkpoints from older implementations are not migrated.

Issue text, comments, source, logs and model output are untrusted. They cannot
authorize arbitrary shell, refs, workflows, feeds or credentials. Discovery
must resolve one ordinary test declaration, class/namespace, existing test
project, and literal issue error. Ambiguous/generated/nested cases stop for a
human. Repair paths are existing regular C# files in the selected project's
explicit project-reference graph; dynamic references do not grant broader scope.
Scheduling, data, and quarantine attributes cannot change.
The conservative lexical guard removes comments, preserves complete attribute
arguments, declaration ownership, imports and conditional directives, and
includes custom collection/parallelism attributes. Ownership follows actual
namespace/type scopes and closing braces, so a test cannot move out of a nested
type while retaining identical opening declarations. Unsupported interpolated
strings and some array/indexer edits require human review rather than relying
on a partial parser as full C# semantic validation.

Candidates run through the fixed `reproduce-flaky-tests.yml`. Its optional
`investigation_issue_url` and `investigation_revision` inputs select an immutable
worker-intent checkpoint, not arbitrary settings or code. When neither input
is supplied, the existing branch-configured manual defaults remain unchanged.

Scoped `ankj/flaky-test-<issue>-run-<round>` refs point at the already published
trusted worker revision. The controller never creates workflow-containing trees.
Trusted setup validates the checkpoint and owned run, derives its matrix and
source revision, and checks the candidate checkout before building. The trusted
workflow/runner stay in the root checkout; candidate source is checked out
separately under `candidate/`. Neither checkout persists credentials, and the
job has read-only repository permissions. No candidate process runs in a
publication job or receives write credentials.
This token boundary alone does not protect the root checkout, configuration or
evidence from candidate code running as the same host user. The trusted
controller and worker reject candidate execution and fix publication by default.
Orchestration tests using a synthetic approved isolation boundary do not prove
that a production candidate run is safe.

The agent job is read-only. Compiler-generated proposal-memory persistence has
`contents: write` to its scoped proposal branch; the generated conclusion job
also requests `issues: write`. Those jobs do not execute candidate code.
Automatic issue-creation/error-report handlers are disabled. The trusted
controller alone publishes findings and source PRs.

A reusable caller must allow `contents: write`, `actions: read`, `issues: write`,
and `copilot-requests: write` for the compiled workflow. These are the combined
job requirements, not the permissions of the model job.

## Evidence and finite limits

The prompt requires checking actual OS, toolchain and test-specific prerequisites,
then bounded local work when feasible. A skip cites actual probes/results and
the concrete blocker, not unchecked assumptions about a cold agent environment.
Source/history analysis continues where possible even when local execution is
unavailable. The skill and supplemental prompt guide diagnosis and experiment
selection; the controller does not require a hosted baseline before repair.
Local results never replace final exact-candidate hosted verification.

A first targeted `reproduce`, `repair` or `finish` proposal supplies `selection`
and `local_precheck` together. No proposal-version change or selection-only phase
is needed. An explicit `reproduce` request chooses `single` mode or quarantined
`project` contention, 1–3 runners and 1–10 iterations per selected OS. Defaults
are single-test mode and three runners × three iterations. Selection, bounded
configuration, quarantine rules and exact frozen source remain validated.
Every requested run yields to the controller and resumes a fresh phase with
authenticated results, not an automatic experiment sequence.

An honest `not-reproduced` finding can follow local/log investigation or any
number of clean requested hosted rounds. It must not contradict an accepted
target failure or an existing candidate; no three-round threshold is imposed.
Findings distinguish model-authored local analysis from accepted hosted execution
counts, including when there are no accepted executions.

When hosted reproduction is requested, target failures must match the issue's
error; a pre-fix pass is not required. A test that failed on every observed
attempt remains repair-eligible, subject to the same isolation, review and
final verification gates. Unrelated failures, build/runner errors, missing or
skipped tests, and missing TRX cannot authorize a candidate.
Reproduction concerns an exact case display name and TRX definition ID on an
exact OS at the frozen source, including across baseline
rounds. Colliding IDs for one display name are rejected before accepting a
batch; OS-specific IDs are not pooled. ID stability is checked, not assumed
for every theory or candidate revision. Mixed passes and matching failures for
the same case/OS establish observed intermittency; a finite all-failed sample
does not prove permanent determinism. Failure
signatures are matched only against TRX `ErrorInfo`, never `StdOut`. Both short
and class-qualified MTP method names resolve to the exact selected method.
Older aggregate-only or display-name-only records cannot supply exact-case
verification coverage. Final verification must cover every selected OS and
every observed target case/definition with at least nine passes and no failures.
Any exact failing cases from requested hosted reproduction must also be covered;
passing other rows, definition IDs or OSes cannot substitute. Without a hosted
baseline, the final original TRX still binds executed cases to the selected
class/method. Model-authored local records cannot fill missing coverage.

Every runner retains each iteration's original TRX, output, process exit and
checkout receipt, including passing iterations. The controller validates exact
run/attempt/source/filter identity and counts original result records, not
summary counters or merged reports. Findings report **requested
runner-iterations** separately from **actual tests**: project-mode iterations
can execute many tests and are not an aggregate test-count cap.
The console tee preserves raw artifact bytes. A process timeout stops the
test's own POSIX process group, or requests exact-PID Windows tree termination
with `taskkill /T /F`. A cleanup-command failure rejects the round; it is not
a passing timeout receipt. These mechanisms do not isolate adversarial candidate
code, and Windows behavior still needs native hosted validation.

Each issue has eight model phases, five worker rounds, two repair attempts,
180 runner-iterations, and twelve hours. Each model has 15 minutes; the existing
reproduction runner has a 60-minute job timeout. Repairs are limited to five
existing files and 800 changed lines. Final verification requires at least nine
target passes per selected OS and observed target case, no failures, and the
requested contention mode. A `repair` proposal may request `mode: project` for
a quarantined target without a prior hosted round. Any previously requested
project-mode reproduction also requires project-mode verification, even if it
was clean. The chosen candidate mode is checkpointed for recovery; older
candidates without it retain project mode when their history requires it.
All worker rounds share the existing cap, including verification. The user's
final investigation-versus-verification accounting is not resolved by this
mechanical limit; it does not grant five investigations plus extra verification.
Exhaustion cannot waive mandatory verification. The final
fresh agent phase must review the exact verified candidate SHA and its files.
Nine passes are finite evidence, not proof that a low-frequency flake is gone.

Child dispatches checkpoint absolute deadlines: thirty minutes for a complete
agent workflow and ninety minutes for a worker, including queue/setup time.
Resumes never reset these or the overall twelve-hour budget. A short controller
exiting or timing out does not itself cancel a healthy worker.
Late observation does not expire work whose exact-attempt jobs all completed
before the child deadline. The controller checks every job's `completed_at`,
not the workflow's mutable `updated_at`; missing, foreign or unfinished job
records cannot establish timely completion. The overall twelve-hour cap still
prevents starting more work.

Cancellation persists its exact owned-run identity before the write, then
reconciles completion without repeating an uncertain request. No results or
new iterations are accepted during cancellation. After thirty minutes without
confirmed completion, the controller posts one explicit blocked finding that
the child may still be active and retains `cleanup-pending`. Independent
recovery continues checking that exact run until it is completed, then records
cleanup and becomes terminal without a second finding or new work.

A malformed checkpoint fails closed before observation or advancement,
including present inactive child, target, cancellation, history and note
records; it is not repaired from model memory. One failed issue does not
prevent other recovery issues from advancing, but the sweep still fails visibly. If Actions ownership
cannot be read, recovery cannot safely cancel the run; restore access or inspect
the exact checkpointed run manually. Missing evidence alone is not a trusted
runner-loss diagnosis and does not trigger an automatic infrastructure retry.
Uncertain finding publication is reconciled by bot-authored marker, never
reposted merely because dispatch cleanup later completes.

CLI E2E selections stop before dispatch with an actionable blocker because their
prebuilt runtime images are not candidate-aware. Existing manual CLI E2E
reproduction is unchanged. Other tests that need unsupported runtime artifacts
or unavailable infrastructure can likewise finish blocked rather than claim
verification against the wrong environment.

Terminal outcomes are `verified`, `not-flaky`, `ambiguous`, `infrastructure`,
`not-reproduced`, `budget-exhausted`, `blocked`, and `cancelled`.
Findings use an automation prefix and a marker tied to issue/base SHA; duplicate
suppression also requires the actual `github-actions[bot]` author. PRs preserve
the repository template and contain only the source candidate, not investigation
workflow changes. Human code walkthrough and security review remain required.

### Findings when no verified fix is reached

Every confirmed non-verified terminal outcome uses the same trusted publisher
to comment on the original issue. The comment leads with a short reason no
verified fix was reached, attempted work, accepted observations, rejected-run
links, and one next action. Detailed run tables, source identities, local
precheck output, model evidence references, and rejected diagnostics are under
a collapsible **Evidence** section. Long detail is truncated for display;
the original checkpoint and artifacts remain the audit source.

The prompt asks a finishing model for bounded `analysis`: `why_no_fix`
(600 characters), one to three `attempted` steps (240 characters each), and
`next_action` (300 characters). Fields must be single-line text.
Model-authored analysis is labelled as not independently established; counts
and run identities come from the controller, not that prose.

If an agent fails, omits or corrupts its proposal, or cannot finish within its
budget, the controller supplies an explicit trusted fallback. Infrastructure,
missing TRX, unrelated failures, invalid candidates, exhausted repair attempts,
cancellation, and interrupted intents likewise retain an actionable reason.
No extra model runs just to summarize failure. Old proposals without `analysis`
remain readable and their explanations are labelled as legacy model text,
not silently promoted into a trusted summary or the new structured analysis.

Rejected attempts are stored separately in `rejected_attempts`, with phase,
bounded diagnostic, available checkpointed run/source identity, and rejected
action where known. They never enter accepted test counts. A model or worker
run link is not proof that its output passed validation. The controller can
publish a fallback even when no valid model analysis exists, provided GitHub
remains writable and checkpoint recovery can run.
Checkpointed run identities are not test results. Interrupted intents without
a run receipt say so rather than linking to a guessed run.

If publication was interrupted, recovery checks for the owned bot marker. If
the remote outcome cannot be established, it records a terminal
`publication_uncertain` blocker and emits an error rather than retrying a
possibly completed comment/PR write. Inspect the checkpoint and remote objects
before any manual remediation: an unconfirmed response does not mean no PR
or comment exists. A GitHub outage, missing write permission, or uncertain
publication cannot guarantee delivery of a comment. Investigation refs and
artifacts are retained for audit; artifact expiry can make an old
investigation unrecoverable.

## Validation and deployment

Issue/comment creation and update dates are retained separately from preparation
time. Old “last 30 days” counts are not current observations. Preparation does
not collect historical quarantine logs; the agent must report what it actually
read and what evidence is unavailable.

Proposals use the required repo-memory artifact pipeline. The unused optional
safe-output upload capability remains configured because the pinned compiler
otherwise auto-adds issue creation when only built-in outputs remain. Its
missing-optional-artifact warning is unresolved, as are observed framework
`chown` and `awf-reflect` read-only-temp warnings. Sandbox permissions and pinned
framework versions are not relaxed to suppress them.

Behavioral tests invoke the production state machine, transport boundaries,
evidence reader and runner. `FlakyInvestigationTests` runs them from
`Infrastructure.Tests` on Windows and Unix. No tests assert workflow/source text.

```shell
python3 -m unittest discover -s .github/scripts/flaky_investigation -p 'test_*.py' -v
dotnet test --project tests/Infrastructure.Tests/Infrastructure.Tests.csproj \
  --no-launch-profile -- \
  --filter-class "*.FlakyInvestigationTests" \
  --filter-not-trait "quarantined=true" --filter-not-trait "outerloop=true"
gh aw compile flaky-test-investigator --schedule-seed microsoft/aspire --validate
bash .github/workflows/lint-handwritten-workflows.sh
```

Use the repository-pinned compiler, currently v0.89.17. Regenerate the lock;
never edit it manually. Compilation and behavioral tests do not establish
hosted model authorization, built-in-token bot activation, action allow-list
acceptance, or successful live reproduction. Hosted validation must also prove
that checkpoint/source/ref publication and the fixed reproduction adapter work
with the built-in job token. No runtime step requires workflow-file mutation.

New dispatch filenames and schedules require default-branch registration.
Premerge validation can use a separately reviewed, temporary existing-workflow
bootstrap that calls the generated reusable agent. The trusted wrapper may set
`FLAKY_AGENT_WORKFLOW=reproduce-flaky-tests.yml` and `FLAKY_AGENT_SHA` to the
immutable agent-wrapper revision, plus `FLAKY_WORKER_SHA` to the immutable
production revision containing the fixed reproduction adapter. These are
trusted deployment settings, never issue/model inputs. Their identities are
frozen in the checkpoint. Production defaults use the canonical agent filename
and the control revision for both agent and worker code.

For premerge validation, publish the exact trusted worker revision to an
upstream ref before the job token creates run refs. In the observed
[before](https://github.com/microsoft/aspire/actions/runs/39673661846) /
[after](https://github.com/microsoft/aspire/actions/runs/39673902818) probe,
readable ancestor access returned HTTP 403 until that registration. This is an
observed activation prerequisite, not a universal GitHub policy guarantee.
Only separately authorized bootstrap setup uses a workflow-capable credential;
runtime jobs still use their job tokens and do not modify workflow files.

An agent wrapper forwards `issue_url`, `issue_number`, and `state_revision`.
The controller derives the number from the URL; preparation checks they agree
before model work. Proposal branches stay in the per-issue namespace:
`ankj/flaky-test-<issue>-proposals-<run-id>`.

Validate the real issue-only flow, exact original artifacts, fresh model
phases, token-dispatched bot activation, recovery and terminal output before
claiming deployment complete. Upstream policy/settings failures are blockers,
not permission to change main or introduce another credential.
