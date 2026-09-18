# CI shepherd workflow loop

The workflow loop observes failed GitHub Actions workflows and coordinates at
most two owned repair items. It does not merge pull requests, approve changes,
rerun workflows, quarantine tests, or repair ordinary test failures.

## Core and scenario boundary

`CiCoordinator` is the shared GitHub CI core. It owns the periodic pass,
scenario registration, persistent item/scenario binding, shared two-item
capacity, worker lifetime and result consumption, durable effect invocation,
common task/issue/PR state, and `WOULD_DO` visibility in no-effect modes.
`GitHubEffectExecutor` owns PREPARED → INVOKING → terminal action receipts and
uncertain-write handling.

`WorkflowFailureScenario` is the only production scenario. It owns workflow
candidate selection, exact priority, job/log evidence, judgment prompts,
workflow assessment, and positive job-level recovery proof. It proposes
effects through `ItemTransition`; it never calls a GitHub mutation. The
`WorkflowLoopManager` name remains as a thin compatibility facade that wires
this scenario into the core. Registration is explicit Python construction:
there is no plugin loader, provider abstraction, external queue, or second
scheduler. A test-only scenario verifies the shared boundary and capacity.

Items are durably bound to their scenario in SQLite. Existing workflow-only
schema-3 stores migrate in place with their item IDs and issue/task/PR links,
binding each prior row to `workflow-failure`. New rows use
`(repository, branch, scenario, case)` identity, so two scenarios can track
distinct cases concerning the same native workflow without ownership takeover.
A persisted binding whose scenario is not registered is rejected rather than
reassigned.

### Workflow priority

The workflow scenario uses exact repository paths:

1. `.github/workflows/ci.yml` — rolling build.
2. Test workflows — `tests.yml`, daily smoke, outerloop, quarantine,
   deployment tests, extension E2E, CLI starter, polyglot, TypeScript SDK/API
   compatibility, flaky-test reproduction, and their reusable test runners.
3. Every other workflow.

Within a tier, item ID provides deterministic ordering. Active work is never
preempted. All candidates are persisted before selection; tracked ownership is
refreshed before priority admission, so a newly completed lower-priority task
releases capacity before a rolling-build failure is considered.

## Responding to a failure

A completed failure is eligible for analysis immediately, even when a newer
run of the same workflow is queued or running. Pending runs are not evidence
of recovery and do not delay repair preparation. Capacity, existing repair
ownership, and human approval gates still apply.

Before a write, the loop checks whether a newer completed execution actually
ran and passed the affected jobs. That positive recovery suppresses obsolete
work. Items saved under the earlier wait-for-a-newer-run policy discard that
wait on their next pass; no state reset is required.

## Safe modes

There are exactly two operational modes: **READ_ONLY** (the default) and
**LIVE** (`--live`). Both refresh GitHub evidence and run restricted local
Copilot judgments. Only LIVE can construct the GitHub actor or invoke an
external effect. The former `--local-judgment` flag is no longer accepted.

READ_ONLY snapshots the canonical `--state-dir` into a separate, owner-only
persistent shadow using SQLite backup, including committed WAL contents.
It never initializes, migrates, or changes logical rows in the canonical
database. An absent canonical database starts an empty shadow.
All observations, worker packets, results, transitions, and proposals belong
to the shadow. This is GitHub-read-only, not filesystem-read-only.

Use `--shadow-state-dir` to name and resume that read-only run. If omitted,
the CLI allocates a unique sibling directory and prints its path together
with the canonical source path. A `watch` creates its shadow once; subsequent
passes reuse it. Reusing an explicit shadow also resumes its local workers
rather than resnapshotting newer canonical state.

Resume requires the shadow root and its coordinator state files (including
the marker and SQLite database) to remain owner-only. Provider-created
descendants may use broader modes beneath that private root; resume does not
change their permissions. Symlinks, hardlinks, and nonregular files are still
rejected throughout the tree.

Inherited worker rows are rehomed under the shadow and lose their PID.
No inherited worker is launched or observed, and executable packet/manifest
files are deliberately not copied. Provenance records why they are unavailable.
Items with unconsumed inherited workers (including terminal results), or
PREPARED/INVOKING/UNCERTAIN actions, stay explicitly **FROZEN**.
They retain canonical operation history without claiming termination or taking
over an invocation. They do not occupy shadow-owned capacity; the shadow can
analyze unrelated items. Canonical task state and live capacity are unchanged.

At the first external effect boundary, the shared effect executor records a
structured **PROPOSED** history entry with the exact live intent and payload.
`status` shows the exact external target and title/body/prompt, not the internal
assessment packet. Identical proposals are deduplicated. They reserve no action
capacity, and terminal local judgments remain available for later shadow passes.
Unrelated items continue. A proposed issue creation cannot fabricate an issue
number to simulate a downstream task.

LIVE uses only canonical state and fresh observations. It cannot use a shadow
as its state directory or promote a shadow proposal/judgment. Run LIVE with
the canonical path when authorized; any needed judgments run independently.
`status` inspects persisted local state without GitHub access.

## Repeated-pass correctness

Every successful poll persists `last_checked_at`, even when no semantic state
changes. `last_progressed_at` and progress history change only for real
progress.

Follow-up assessments are bound to the task, PR head/base, check state, draft
state, and issue ownership they evaluated. Unchanged non-action or stale
results remain stable across process restarts. Changed semantic target evidence
gets a distinct worker context fingerprint without weakening duplicate
reservation protection.

A PREPARED effect is resumable after a transient read failure. Its worker
result remains unconsumed until a later healthy pass confirms or supersedes the
effect. INVOKING and UNCERTAIN effects are never retried. The final follow-up
guard rechecks human issue ownership and draft state; either moves the item to
human waiting without changing the task ID or follow-up count.

Late human ownership, draft conversion, green checks, or changed targets
durably supersede an existing PREPARED follow-up. This releases capacity and
consumes the retained proposal without invoking the actor. Copilot-only
assignment remains eligible; when Copilot and a human are both assigned, human
ownership takes precedence.

Queued and running workers remain attached to their item even when newer
failure evidence arrives or recovery starts a new episode. The new evidence
waits for that process to terminate. A terminal stale worker and any
never-invoked PREPARED action are then durably recorded as superseded evidence
before exactly one worker can be queued for the current evidence.

The workflow scenario adopts one verified canonical issue before creating the
immutable round-zero packet. Human- or Copilot-owned issues become passive
observation, while ambiguous or unavailable searches never guess. A successful
later attempt of the same run can prove recovery. A new completed failure after
recovery advances the same scenario case to a new episode; any still-live task
from the previous episode retains capacity until terminal.

Packet preparation failures become durable `needs_attention` state and a
degraded pass rather than queued work without a reservation. Follow-up
preparation failures also persist the exact task/PR/check/ownership target, so
an unchanged restart does not retry; meaningful target changes may be assessed
again.

The local Copilot judgment worker receives only the `view` tool and cannot
mutate GitHub.
`--model` and `--reasoning-effort` select that local runtime explicitly; they
do not configure the later cloud task.

`--live` permits writer actions only when
`--allow-write-repository` appears exactly once and exactly matches
`--repository`. There is no implicit upstream write grant.

```bash
PYTHONPATH=scripts python3 scripts/workflow_loop.py pass \
  --repository microsoft/aspire \
  --branch main \
  --state-dir /absolute/path/to/canonical-state \
  --shadow-state-dir /absolute/path/to/readonly-run \
  --model gpt-5.6-sol \
  --reasoning-effort medium

PYTHONPATH=scripts python3 scripts/workflow_loop.py pass \
  --repository radical/aspire \
  --branch main \
  --state-dir /absolute/path/to/fork-state \
  --workflow-id 12345 \
  --live \
  --allow-write-repository radical/aspire
```

Run these commands from `.ci-shepherd-build/`. The first is read-only against
upstream; the live example targets a fork and requires separate operator
authorization. Neither command is implied by running the local test suite.

`watch` starts the same pass implementation every five minutes. Pass duration
is deducted from the next sleep, so the interval is start-to-start rather than
five minutes plus the prior pass duration. Each GitHub read gets one HTTP
attempt; a failed read remains explicitly unavailable and is retried by the
next pass rather than delaying the current pass with transport retries.

```bash
PYTHONPATH=scripts python3 scripts/workflow_loop.py watch \
  --repository microsoft/aspire \
  --branch main \
  --state-dir /absolute/path/to/canonical-state \
  --shadow-state-dir /absolute/path/to/readonly-run
```

`SIGINT` and `SIGTERM` stop future scheduling. A signal received during a pass
lets that pass finish; it does not terminate or expire a judgment worker.
A one-shot `pass` exits nonzero and prints `status=degraded` when any typed
GitHub read is unavailable. `watch` reports the same error but continues to the
next scheduled pass so the read can recover.

## Read-only observation performance

The 2026-09-17 baseline used independent CLI processes against
`radical/aspire` `main` on macOS 26.6.2 (25G83), with Python 3.14.7,
`gh` 2.98.0, and Copilot CLI 1.0.86-2. The repository exposed 32 active
workflows and no active failures. Each empty observation made 35 successful
GETs: repository identity, branch identity, workflow inventory, and one run
window for every active workflow.

| Pass ID | Wall time | GETs | Assignments |
|---|---:|---:|---:|
| `d3925b4b0ff74d4885b589698d4ffc94` | 44.111s | 35 | 0 |
| `30136faaa6b5457985284ab3e0279d06` | 69.317s | 35 | 0 |
| `4dc537bbd1e64fc8be07bb37c4b3ea93` | 112.155s | 35 | 0 |

The three-pass median was 69.317s (44.111–112.155s). A fourth instrumented
pass, `18e5e4f662764ca4a3226fad862c4f68`, took 36.556s. Individual
`_get_json_response` calls accounted for 36.527s of 36.564s instrumented wall
time, with the slowest workflow-window calls taking 2–3.097s. Its raw profile
is retained as `readonly-fork-phase-profile.json` in the validation session
artifacts. No request retried or returned an error, so one-attempt reads enforce
the unavailable-read contract but do not explain these timings.

The retained change affects only the 32 independent workflow-window reads:
after repository, branch, and workflow-inventory verification, read at most
four windows concurrently, then restore workflow and error ordering by workflow
ID. This keeps complete workflow coverage and the same 35 requests, adds no
service or per-worker agent overhead, and does not parallelize details, logs,
or mutations.

Three post-change read-only passes used the same repository, branch, workflow
inventory, and tool environment:

| Pass ID | Wall time | GETs | Assignments |
|---|---:|---:|---:|
| `bde7b86783da42be99f1f9d3f5f5aa3e` | 11.605s | 35 | 0 |
| `6c17d820d26f46329df733d9329c4b99` | 9.717s | 35 | 0 |
| `38861f9c397f481f93e6f7c0a6b58f9d` | 10.832s | 35 | 0 |

The post-change median was 10.832s (9.717–11.605s), compared with the initial
69.317s median (44.111–112.155s). Across these three passes the loop made 105
successful GETs and created no items, workers, actions, or writer audit. These
numbers demonstrate empty-inventory monitoring performance only; they do not
measure the repair end-to-end path.

Unit coverage proves four-way overlap, deterministic output and error ordering,
per-workflow unavailable isolation, no job/log/detail overfetch, and accurate
request counts. These empty-observation measurements do not validate repair
judgment quality or the issue/task/PR/recovery path; those remain separate
repair end-to-end checks.

## Local judgment validation

Two restricted `gpt-5.6-sol`/high workers ran through the real packet
preparation, reservation, launch, lifetime, JSONL extraction, and strict result
parser on macOS 26.6.2 with Copilot CLI 1.0.86-2. The GitHub DTOs were
synthetic, while their log excerpts came from actual executions of the local
fixture:

- The build/automation `KeyError: 'output_dir'` was classified `assign` with
  exactly its failed job ID and a nonempty repair request.
- The unittest assertion failure was classified `defer_ordinary_test` with no
  in-scope job IDs and a null repair request.

Both workers exposed exactly the `view` tool, executed no tools, released their
lifetime locks, and exited; no action row or GitHub read/write occurred. This
validates local judgment transport and
classification for the supplied evidence, not the live pipeline repair path.

The first attempt exposed that terminal text rendering could wrap content and
that the prompt did not state exact typed identity fields. The worker now
requests the CLI's documented JSONL output, extracts exactly one final
assistant message, and applies the same strict result parser. The prompt names
the exact identity, evidence, and job-ID constraints. Because
`--available-tools=view` does not remove tools from user-configured extensions,
each worker uses an owner-only empty `COPILOT_HOME` under its private packet
directory. It inherits supported authentication without copying user
configuration, plugins, MCP definitions, or extension files. JSONL usage
metadata verifies that the resulting tool list is exactly `view`.

When a verified issue is bound or uniquely adopted, judgment preparation reads
a bounded immutable issue context: title up to 512 characters, body up to
16 KiB, sorted unique labels, and at most 20 comments of 8 KiB each.
Truncation, pagination, and read failures are explicit. Included content gets
exact `issue:<number>` and `comment:<id>` evidence IDs.

Fixed safety rules precede `<untrusted-issue-context>`, whose string values are
JSON encoded and delimiter characters escaped. Exact item, episode, evidence,
job allowlists, decision enum, and output schema follow the closing delimiter.
Issue prose cannot change identity, tools, capacity, freshness, effect
authority, retries, or recovery. Cloud task prompts use only the validated
local result and freshly verified GitHub targets, not issue instructions.
The final prompt is additionally capped at 200,000 UTF-8 bytes. Issue fields
and comments are deterministically shortened to fit after reserving space for
trusted instructions and the output schema; truncation flags and comment
evidence IDs describe only the content actually retained. Malformed issue
fields become typed unavailable evidence and degrade only that item rather than
aborting the pass.

The first real fork failure also exposed a prompt-selection defect. Its complete
10,890-character job excerpt placed `KeyError: 'output_dir'` at offset 8,626,
while the prompt used the first 4,000 characters and omitted the failure.
Prompt construction now uses the existing diagnostic-aware bounded preview,
which keeps a contiguous window containing the traceback and nearby context.
It reports source truncation separately from prompt excerpting; API byte limits
are explicit. Failed-job log reads retain at most 32 KiB in memory: a 4 KiB
head, up to 20 KiB of strong diagnostic lines selected while streaming, and an
8 KiB tail. Responses larger than that remain marked truncated/incomplete.
A single log line is bounded independently; an oversized line produces an
explicit read error, and selector failures always terminate and reap the
reader subprocess. A final diagnostic fragment without a newline is retained.
A fixture-shaped regression verifies the error and cleanup are present and the
complete prompt remains within 20,000 characters. Saved 452 KiB–2.4 MiB logs
retain the SDK, model-budget, and missing-package failures in the bounded
prompt evidence.

The complete Python suite passed 2,593 tests after the modular extraction.
The live fork fixture is published on a dedicated test branch, and its intentional
configuration failure was observed through GitHub Actions. Initial publication
required using the existing stored fork-owner credential rather than an
injected token lacking workflow scope; no global authentication settings
were changed. The paid fork cloud task was cancelled and must not be restarted;
the cloud-task/PR/recovery lifecycle remains unvalidated. No upstream mutation
has occurred.

## Persistence and ownership

For local judgments, the manager creates the canonical private worker paths,
atomically persists the exact request and trusted model/effort manifest, and
only then reserves capacity in SQLite. Launch records a durable launch attempt
before `Popen`. A cold manager resumes an unattempted queued worker from that
immutable packet; it never reconstructs the prompt from newer observations.
Worker identity includes the judgment round, allowing the initial judgment and
two bounded follow-ups for the same stable failure evidence while rejecting a
duplicate round. A terminal result is marked consumed only after its transition
or action receipt is durable, so restarts retain pending results without
replaying historical ones. Failed and invalid worker results remain unconsumed
so their `needs_attention` state stays sticky on every unchanged pass without
launching a replacement.

The wrapper and synchronous Copilot child inherit the same POSIX lifetime-lock
descriptor. Capacity remains occupied while that lock is active, regardless of
PID, elapsed time, or an early result file. Local workers therefore require the
repository's supported POSIX `flock` and `O_NOFOLLOW` semantics.

Writer intents are persisted before invocation. An `INVOKING` action left by a
different pass/process is classified as uncertain and is not retried
automatically. Confirmed task IDs are bound directly to the item. Follow-up
judgments retain the exact task ID plus pull-request head SHA, head/base refs,
and observation time; no issue comments or legacy assignee mutation are used.
The state store binds repository, branch, and the canonical workflow scope
(`all` or the sorted explicit IDs). Every command, including `status`, must use
that same scope. This unreleased implementation rejects older state schemas;
use a fresh state directory for validation.

Every metadata-complete failure is persisted for status visibility even when
both owned-work slots are occupied. Job and log enrichment remains deferred
until that item is admitted. The first-assignment latency uses the initial
task's confirmed receipt time; follow-up receipts do not overwrite it.

## Operator attention and gates

Manual attention is required when a worker lifetime ends without a valid
terminal envelope, output is malformed or belongs to another request, an
invoked write has an uncertain outcome, authoritative reads are unavailable,
the owned task or pull-request identity changes, or the two-follow-up budget is
exhausted.

Human review, CI completion, and merge remain explicit gates. A green or merged
repair pull request does not close the item; the loop waits for authoritative
recovery on a newer primary-branch workflow run.

Use persisted status even when GitHub is unavailable:

```bash
PYTHONPATH=scripts python3 scripts/workflow_loop.py status \
  --repository microsoft/aspire \
  --branch main \
  --state-dir /absolute/path/to/readonly-run
```

The summary shows local activity, waits, last checked/progressed timestamps,
known run/issue/PR links, explicit task IDs, pass duration, GitHub request
count, capacity, and time to first confirmed assignment. Detailed worker
stdout, stderr, usage, and terminal envelopes remain in the item's private
worker directory.

The shadow's `workflow-loop.sqlite3` retains complete proposal history and
`github-reads.jsonl` records reads. A read-only run creates no
`github-writes.jsonl` and confirms no assignments. A successful pass is not
evidence of repair or recovery; inspect unavailable reads, frozen inherited
operations, and proposed external boundaries in `status`.
