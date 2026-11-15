# Proposed design: fewer repeated decisions, stronger execution

Status: proposal. No new automation, script, skill, storage backend, or display is
implemented by these notes.

## Keep the useful separation; simplify what happens between roles

Start with the existing four responsibilities rather than adding more agents.
The immediate problem is not missing role names. It is repeated bookkeeping,
ambiguous handoffs, routine questions, and execution routes that were never
demonstrated end to end.

```text
CI evidence -> monitor -> prioritized incident
                              |
                            fixer <-> independent reviewer
                              |
                    mark ready + request review
                              |
                       human approval/merge
                              |
                     verify affected CI health

Supervisor: resolve missed actions and broken execution paths across this loop.
Manager: change the operating design and handle explicit owner-only decisions.
```

The fixer owns delivery through the real GitHub readiness transition. The
supervisor owns making blocked work actionable, not writing increasingly detailed
reports about it. The reviewer checks work and samples whether supervisor remedies
actually helped; it need not re-audit every unchanged blocker on every pass.

Event delivery can accelerate this loop only when supported and configured.
The inspected group still reported a GitHub administration-access requirement
for event delivery. Keep scheduled reconciliation as the fallback; do not design
around an event channel that is not working.

## Three layers

| Layer | Put here | Do not put here |
| --- | --- | --- |
| Automation prompt | Outcome, authority, task selection, completion conditions, essential safety invariants | Full procedures, growing incident history, repeated explanations |
| Repository skills | How to diagnose, reproduce, repair, hand off, and resolve a specific interruption | Credentials, grants, hidden state, unconditional permission to run anything |
| Tested tools/scripts | Collection, parsing, state validation, change detection, rendering, conditional updates | Autonomous scope expansion or unsupported platform APIs |

Use existing skills first. Add a small group-operations skill only for procedures
not already covered: continuing interrupted work, interpreting execution evidence,
and recording verified handoffs. Extend existing CI/fix skills where the lesson
is generally applicable instead of copying them into a group-specific skill.

Suggested mechanical helpers, after inspecting prior art:

1. **Snapshot collector:** read relevant GitHub and run state once, preserve source
   times, and emit normalized facts with explicit missing/error states.
2. **State transition validator:** validate intended transitions and required
   evidence; use an actually supported conditional-write adapter.
3. **Status renderer:** produce a readable work table and chronological event
   view from the same records.
4. **Change detector:** identify new evidence, changed PR heads, completed external
   runs, and genuinely due actions so agents do not rebuild the same context.

Repository scripts do not automatically have access to hosted artifacts or
session APIs. Verify the available adapter and authorization before implementation.
If no script-capable adapter exists, keep that I/O in selected agent tools and
make validation/rendering pure functions over supplied data.

## Yes, keep a ledger, but keep it small

Use three kinds of durable records:

| Record | Contents | Update pattern |
| --- | --- | --- |
| Current work | Source failure/branch, acceptance, responsible automation/session, stage, latest result, next action, actual blocker | One bounded record per incident |
| Events and evidence | Started action, external operation/run, result, readiness transition, handoff, uncertainty | Separate immutable records |
| Improvement findings | Repeated mistake, evidence, proposed remedy, responsible reviewer, disposition and later outcome | Small open list with separate closed history |

The current record should link evidence, not embed full logs or transcripts.
Keep routine indexes small and derived; they are not an alternative source of
truth for incident ownership. Separate short-lived exclusive execution from
longer-lived responsibility for a PR.

Every work record needs enough information to resume without rereading an entire
conversation: what was accepted, which branch and head, what actually happened,
what remains unknown, and the next permitted step. Store queued/admitted/started
times separately from progress and completion.

Retain current transfer constraints: inline artifact operations are limited to
32 KiB, and hot records need transition reserve within that limit. Use supported
file/chunk paths for larger evidence, not bulk base64 through model text. Preserve
conditional writes and full required verification in helpers rather than asking
an LLM to hand-assemble transfers on every pass.

Do not keep adding per-pass copies of the same narrative. A cheap unchanged-state
observation can reference the existing action. It must not reset the blocked age
or make an old request look newly productive.

## State and event display

Proposed main work table:

| Work | Current state | Responsible actor | Last useful result | Next action | Waiting since |
| --- | --- | --- | --- | --- | --- |
| Interrupted repair | Branch modification waiting; diagnosis available | Fixer; manager for original-session access | Reviewer finding consumed | Verify original command stopped; independently validate repair | Source timestamp |
| Validated change | Awaiting human approval | Requested maintainers | GitHub non-draft state verified | Human review and merge | Ready-for-review event |
| Windows diagnostic | Destination online; execution not verified | Manager/fixer routing | Environment heartbeat received | Route one authorized named diagnostic and inspect result | Request timestamp |

These are generic illustrations of process states, not live work items.

The event view should show timestamp, actor, action, result, and a source link.
For example, a hypothetical event sequence:

```text
09:00:00  Fixer       Started a diagnostic, one runner.
09:03:00  GitHub CI   Finished: selected cases passed; failure not reproduced.
09:07:00  Fixer       Scheduled opportunity to inspect the result.
```

The third line is a schedule, not a claim the result was consumed. The renderer
must make that distinction visibly. Details can expose run/session/operation IDs,
but the main explanation must name actors and the concrete missing action.

Publish public issue updates on material changes, with a bounded freshness
heartbeat as an adjustable policy. Keep internal observation times without
rewriting the public body each pass.

## Handle interrupted work explicitly

| Situation | Action |
| --- | --- |
| Worker asks a question already answered by policy | Original-task handler supplies that policy through an authorized response route; prevent the question in future prompts |
| External CI is running | Save its exact identity, release local execution resources safely, and inspect on completion or the next due pass |
| Old command may still modify a branch | Block conflicting mutation; inspect original execution; continue isolated diagnosis |
| Original result is inaccessible | Name the exact access failure and one actionable operator request; do not repeat resumes or invent successful settlement |
| Review is complete but PR remains draft | Fixer performs and verifies the readiness transition if authorized and gates still pass |
| A skill offers a supported alternative | Use it within authority before escalating an environment problem |
| Nobody can receive a recovery request | Treat missing delivery as the problem; do not label the work as being handled |

A deadline should trigger diagnosis of missing progress, not a duplicate worker,
forced claim expiry, or automatic termination.

## Windows: demonstrate the route before expanding the group

The owner already offered a remote Windows machine. First prove one authorized,
isolated diagnostic can be routed to it and its result returned to the consumer.
Record which session actually ran there. Test read-only prerequisites before
installing or changing anything.

For durable unattended Windows work, compare:

- Explicit environment selection for a bounded child, if the fixer's actual
  runtime supports it.
- A narrowly scoped Windows executor with that environment saved as its default,
  if the child route is unavailable and the workload justifies another automation.
- Existing task-specific GitHub CI routes where already authorized.

A Windows executor is an option, not an approved addition. It would require
explicit configuration and activation approval, a chosen model, isolation rules,
and offline behavior. Do not move all four automations onto the owner's machine.
Do not repeatedly ask the owner to offer a machine that is already connected.

## Implementation sequence and proof

1. **Capture policy now:** source-branch targeting, autonomous routine decisions,
   required skill use, real PR readiness actions, and plain-language blockers.
   Reconcile already-waiting tasks separately; prompt edits are prospective.
2. **Prove missing routes:** original-task answer/inspection and Windows diagnostic
   delivery. Report exact failures instead of adding more prompt prose around
   an unavailable operation.
3. **Build the small shared helpers:** reuse existing collectors, add behavioral
   tests for state/renderer logic, and preserve supported storage boundaries.
4. **Simplify prompts around those procedures:** compare effective changes against
   current definitions, preserve unrelated settings, and verify applied revisions.
5. **Evaluate the loop:** measure whether due work receives a useful action and
   whether fixes improve the affected CI, not whether every role produced a report.

Focused regression cases should cover branch targeting, a PR described as ready
while still draft, duplicate reviewer requests, unknown external outcomes,
queue-delay versus execution-delay reporting, a passing run with zero executed
tests, unavailable Windows child creation with a permitted .NET CI alternative,
and unchanged blockers retaining their original age.

Do not test literal YAML, prompt, or source strings as a substitute for behavior.
Test helpers where decisions run and validate platform-dependent behavior through
an actual authorized execution.

## Success criteria

For a stated observation window, report:

- Eligible due incidents receiving useful action versus all eligible due
  incidents, including those never selected.
- Delay from a result becoming available to the responsible worker using it,
  separating scheduling/queueing from execution.
- Validated owned PRs actually marked ready and given reviewer requests.
- Repeated unchanged checks and coordination overhead, where measured.
- Duplicate or conflicting effects, unintended scope expansion, and weakened
  test coverage as guardrail failures.
- Subsequent eligible CI executions containing each fix, with first attempts,
  branch, platform, and actual test exposure preserved.

Baseline values are unknown until collected. Historical workflow failures with
unknown causes cannot be labeled infrastructure/flaky failures. Keep post-fix CI
observation separate from the immediate check that a worker resumed successfully.
