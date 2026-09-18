# CI shepherd workflow loop

The workflow loop observes failed GitHub Actions workflows and coordinates at
most two owned repair items. It does not merge pull requests, approve changes,
rerun workflows, quarantine tests, or repair ordinary test failures.

## Safe modes

`status` reads only the local SQLite state. `pass` and `watch` default to
read-only GitHub observation: they may update local observations, but they do
not start local judgment workers or perform GitHub mutations.

`--local-judgment` permits the view-only local Copilot judgment process. The
worker receives only the `view` tool and cannot mutate GitHub.
`--model` and `--reasoning-effort` select that local runtime explicitly; they
do not configure the later cloud task.

`--live` permits writer actions only when
`--allow-write-repository` appears exactly once and exactly matches
`--repository`. There is no implicit upstream write grant.

```bash
PYTHONPATH=scripts python3 scripts/workflow_loop.py pass \
  --repository radical/aspire \
  --branch main \
  --state-dir /absolute/path/to/fork-state \
  --workflow-id 12345

PYTHONPATH=scripts python3 scripts/workflow_loop.py pass \
  --repository radical/aspire \
  --branch main \
  --state-dir /absolute/path/to/fork-state \
  --workflow-id 12345 \
  --local-judgment

PYTHONPATH=scripts python3 scripts/workflow_loop.py pass \
  --repository radical/aspire \
  --branch main \
  --state-dir /absolute/path/to/fork-state \
  --workflow-id 12345 \
  --live \
  --allow-write-repository radical/aspire
```

These are fork-only validation examples. Live fork execution is a separate
operator decision and is not implied by the local test suite.

`watch` starts the same pass implementation every five minutes. Pass duration
is deducted from the next sleep, so the interval is start-to-start rather than
five minutes plus the prior pass duration. Each GitHub read gets one HTTP
attempt; a failed read remains explicitly unavailable and is retried by the
next pass rather than delaying the current pass with transport retries.

```bash
PYTHONPATH=scripts python3 scripts/workflow_loop.py watch \
  --repository radical/aspire \
  --branch main \
  --state-dir /absolute/path/to/fork-state
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

The first real fork failure also exposed a prompt-selection defect. Its complete
10,890-character job excerpt placed `KeyError: 'output_dir'` at offset 8,626,
while the prompt used the first 4,000 characters and omitted the failure.
Prompt construction now uses the existing diagnostic-aware bounded preview,
which keeps a contiguous window containing the traceback and nearby context.
It reports source truncation separately from prompt excerpting; API byte limits
are unchanged. A fixture-shaped regression verifies the error and cleanup are
present and the complete prompt remains within 20,000 characters.

The complete Python suite passed 2,582 tests after this correction. The live
fork fixture is published on a dedicated test branch, and its intentional
configuration failure was observed through GitHub Actions. Initial publication
required using the existing stored fork-owner credential rather than an
injected token lacking workflow scope; no global authentication settings
were changed. The real cloud-task and repair-PR lifecycle remains under
validation. No upstream mutation has occurred.

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
  --repository radical/aspire \
  --branch main \
  --state-dir /absolute/path/to/fork-state
```

The summary shows local activity, waits, last checked/progressed timestamps,
known run/issue/PR links, explicit task IDs, pass duration, GitHub request
count, capacity, and time to first confirmed assignment. Detailed worker
stdout, stderr, usage, and terminal envelopes remain in the item's private
worker directory.
