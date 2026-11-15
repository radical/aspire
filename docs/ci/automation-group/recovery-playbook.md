# Recover interrupted or stuck work

Status: proposed procedure and implementation plan, November 15, 2026.
Not an applied prompt change, deployed skill, cancellation grant, or claim that
the existing stuck sessions have recovered.

## Outcome

Group-owned work should either resume safely or reach a specific, actionable
execution/access failure. It must not consume repeated automation passes merely
to rediscover the same unknown command outcome. Preserve the original task and
acceptance criteria; stopping a recovery attempt does not complete the task.

Keep the existing roles. The fixer records resumable work and owns delivery.
The supervisor diagnoses missing progress and coordinates permitted recovery.
The reviewer checks that recovery actually resumed useful work without duplicate
effects. The monitor reports the concrete state and actor.

## What the platform currently establishes

The hosted tool catalog inspected around 06:30 UTC describes:

| Capability | What it establishes | What it does not establish |
| --- | --- | --- |
| `get_session` | Persisted lifecycle and update times within a coordination grant | Live process activity, useful progress, or a command having stopped |
| Run ledger and captured history | Recorded run identity, turns, commands/results captured in that history | Current process state or absence of a still-running external action |
| `reconnect_session` | For selected, authorized original bindings: inspect; reconnect; one fixed reconciliation-only resume when original work/commands are idle or settled | Stop a hung command, restore revoked access, answer arbitrary questions, replay business work, or create a replacement |
| Artifact compare-and-swap and fences | Conditional artifact writes within their documented atomic boundary | Universal protection against an old worker later pushing to GitHub |
| Session messages | Request delivery or queueing, with exact operation identity | Receipt, cooperation, termination, or successful recovery |

The inspected catalog exposes no general command-status or session-cancellation
primitive. Host-local command controls may exist for a worker's own commands;
they are not automatically available to its supervisor on another machine.
The manager's frozen selection does not include `get_session` or
`reconnect_session`, even though those names appear in automation definitions.

A separate Copilot task-status tool was discovered, but the attempted read
returned "MCP tool is not registered." That is a tool-availability failure, not
evidence that the task is missing, running, stopped, or still waiting for input.
Do not repeatedly invoke it unchanged.

These are current interface observations, not claims that the product can never
provide stronger controls. Demonstrate the route in the actual executing session
before depending on it.

## 1. Record enough information before starting work

Extend existing per-work records additively; do not introduce a second ownership
ledger. Record:

- Original goal, acceptance criteria, source/target branch and expected head.
- Responsible automation, immutable revision, session and exact command or
  external-operation identity.
- Operation kind: local read/test, local modification, or remote mutation;
  exact resources it can affect and the result used to verify completion.
- Stage, start time, last useful result and its evidence timestamp, expected
  next milestone, and when to check again.
- Existing checkpoint, unresolved effects, recovery action and its result.

Persist operation intent before external effects. If the process stops between
intent and receipt, keep that uncertainty; an absent receipt proves nothing.
Store full evidence separately and retain exact references.

Hot records, including at least 4 KiB transition reserve, remain within 32 KiB.
Pin read ETags and reconcile conditional-write conflicts. Do not migrate or
truncate historical records as part of an emergency recovery.

## 2. Decide whether the worker is progressing

There are two clocks: elapsed time in the stage, and time since the last useful
result. Neither a heartbeat nor another rewritten status message resets the
second clock. Timestamp the evidence, not the time it was reread.

Useful progress includes completed build/test phases, a new diagnostic result,
a narrowed hypothesis with supporting evidence, or a verified checkpoint.
Repeated identical logs, lease renewals, tool retries and "still investigating"
are not useful progress. CPU activity or growing output can support a diagnosis
but cannot by themselves prove the task is advancing.

Proposed defaults, not automatic kill limits:

- Before a long action, set its next expected milestone using the command's
  documented timeout or known behavior. Without that evidence, use 15 minutes
  as a first inspection threshold, not a declaration that it is hung.
- Inspect on the first supervisor opportunity after a milestone is missed.
  The current supervisor cadence is 30 minutes; queueing can add delay. A
  five-minute detection guarantee would need a separately approved schedule or
  non-agent runtime watchdog, not just prompt wording.
- If useful results are advancing, record the evidence and a justified next
  milestone. Also investigate an exceeded stage-duration estimate: an agent
  can stay busy on irrelevant work.
- After two eligible checks without progress, require a different executable
  recovery action or a specific unavailable-capability result. Do not rename
  the same request, reset its age, or silently expand its deadline.

Workers should checkpoint at stage boundaries and before external waits.
A responsive worker can update during long work; a blocked worker cannot be
trusted to diagnose its own hang. Independent supervision remains necessary.

## 3. Classify before acting

| Observed situation | Next action |
| --- | --- |
| Work not delivered; capacity or quota is blocking admission | Check capacity/backlog evidence. Do not start another copy or call it a command hang. |
| Command has useful progress | Continue with an evidenced milestone and next check. |
| External CI is still running | Retain its exact run ID, safely settle local commands, end the waiting turn, and inspect on completion or next due pass. |
| An input asks a routine question answered by policy | Supply the recorded policy through an authorized original-task input route. If that route is absent, report missing input delivery, not a need for the owner to decide again. |
| Command finished and result is available, but the worker did not consume it | Verify the exact result and arrange its consumption. Do not repeat the command. |
| Command stopped, but its outcome is missing | Inspect the authoritative result and affected resources; finalize the checkpoint from evidence. |
| Command appears hung | Inspect the exact command and dependencies. Ask for cooperative checkpoint/stop only through an authorized route; verify what happens. |
| Confirmed hung command cannot cooperate | Use an explicitly authorized exact-command/session cancellation control, if supported. Cancellation acceptance is not proof of termination. |
| Original execution or effect cannot be inspected | Try a different authorized evidence source once if it can answer the question. Otherwise record the missing control and stop that recovery attempt, not the entire group's work. |

Network silence is not proof of a hang. A quiet build may be healthy; a verbose
agent may be looping. Recheck actual conditions before intervention.

## 4. Resolve unknown results by operation type

For a local read or isolated test, recovering or rerunning it may be inexpensive,
but first establish that it truly has no relevant external effects. Tests can
start services, deploy resources, or modify shared state. Do not classify them
as harmless merely because the command is named "test."

For remote mutations, read the affected resource:

- A push: inspect the intended branch/head and expected commit.
- A PR creation: inspect the exact repository/head/base and existing PR.
- A workflow dispatch: reconcile the original dispatch/run evidence.
- An artifact write: read the current revision and compare the original exact
  bytes and version, observing the tool's operation-reconciliation contract.

A matching result can establish that the action succeeded despite a lost reply.
An absent result does not establish that the old command cannot still complete.
Account for all outstanding effects before permitting a conflicting successor.
Do not require a dead worker to create one particular receipt file when other
authoritative evidence already proves the result.

## 5. Resume or replace only across a verified safety boundary

Prefer continuing the existing owner from its verified checkpoint. The current
reconnect tool's fixed resume only finalizes existing work; it is not a general
repair-resume command.

A replacement that could affect the same branch/resource needs:

1. Authoritative evidence that the original execution can no longer perform
   conflicting writes, or an enforced resource-specific mechanism that rejects
   its future writes.
2. Reconciliation of already-issued external operations and any partial changes.
3. Preserved original acceptance, source evidence, current head, pending feedback,
   commands/results, and unresolved non-conflicting work.
4. A valid new conditional claim under the existing ownership protocol.
5. A verified useful result from the resumed worker and consumer acknowledgement.

Stopping the conversational turn does not necessarily stop its child processes
or external workflow. Releasing a claim, disconnecting a session, or observing
a lifecycle flag is not the required safety boundary. GitHub prechecks are not
atomic fences, and older workers cannot be assumed to honor a new prompt rule.

If that boundary cannot be established, continue source inspection and isolated
diagnosis without mutating the contested resource. Preserve the incident as
unfinished; do not create another PR for the same work to evade uncertainty.

## 6. Stop the endless recovery loop, not the task

Keep one recovery action identity, the methods attempted, exact results, and
the event or changed condition that could make another attempt useful. Honor
each tool's original-operation retry contract; never create a new operation
identity to evade an uncertain result or a one-resume limit.

If no authorized executor can inspect or stop the original execution:

- State "Recovery unavailable: the supervisor cannot inspect/stop the old
  worker" with the actual missing operation, intended recipient and delivery
  result. Do not say that an unspecified "platform owner" is handling it.
- Route one durable, actionable capability request through a demonstrated
  channel. If none exists, report that missing channel explicitly.
- Keep the work in the unresolved inventory, preserve its age and evidence,
  and continue non-conflicting work.
- Reopen expensive recovery only after changed access, a new result, confirmed
  termination, or another concrete event. A low-cost periodic summary can
  retain visibility without repeatedly reading the same full histories.

This is failure containment, not successful autonomous recovery. The missing
platform control still needs implementation or configuration.

## 7. Teach the procedure and implement the mechanics separately

Create one reusable `automation-recovery` skill from this playbook, with the
decision table, operation-specific checks and worked examples. Prompt hooks
should stay short:

- Fixer: record resumable state, classify effects, and follow the skill when
  interrupted; keep delivery responsibility after ending a turn.
- Supervisor: inspect overdue or non-progressing work, choose a supported
  recovery action, and verify resumption; no repeated unchanged requests.
- Reviewer: sample recoveries, mistaken hang classifications and missed stalled
  work; privately report the mechanism and whether the remedy worked.
- Monitor: show the actual actor, last useful result, concrete wait reason and
  next action, not internal jargon.

Deploy the skill where workers actually run and pin/verify its version before
shortening live prompts around it. A file on this documentation fork branch
is not automatically available to microsoft/aspire automation runs.

Keep classification, deadline arithmetic and display rendering in small
behaviorally tested helpers over supplied data. Let authorized tools perform
I/O; do not invent shell-accessible hosted APIs or credentials. Before adding
helpers, check for existing equivalents and the repository's test conventions.

Required runtime capabilities to demonstrate or build:

1. Exact command inspection with meaningful progress/terminal evidence.
2. Authorized cooperative input/checkpoint/stop delivery and acknowledgement.
3. Exact cancellation with verified execution termination and child-process
   scope, plus a way to prevent stale executors issuing future writes.
4. Durable operation-result lookup and safe checkpoint continuation.
5. A recovery request destination that actually accepts and executes requests.

These are capability requirements, not invented tool names or grants already
available. Narrow cancellation authority and its scope need explicit approval;
never translate it into broad process cleanup, arbitrary session termination,
or cancellation of another contributor's work.

## Proof and improvement

Exercise the behavior before depending on it:

| Case | Required result |
| --- | --- |
| Quiet, progressing command | Not killed; meaningful progress retained |
| No useful progress, repeated log noise | Diagnosed as suspected stall, not healthy |
| Result lost after successful push | Existing commit used; no duplicate push/work |
| Cancellation accepted but process still active | No conflicting successor |
| Worker stopped but external dispatch still pending | External operation reconciled before replacement |
| Expired claim with a live old writer | No takeover |
| Routine input wait | Existing policy delivered or exact delivery failure recorded |
| Denied/unregistered inspection tool | Capability failure, not task-state conclusion |
| Unchanged inaccessible original result | No repeated expensive retry; incident remains visible |
| Changed head or concurrent state update | Reconcile rather than overwrite |

Pure helper tests cannot prove cancellation, revocation or original-session
continuation. Verify those with an explicitly approved disposable worker:
first a successful completion with a dropped result, then an intentional hang.
Confirm actual process/effect state and resumed work, not merely a green tool
response. Do not use a production incident as the first cancellation experiment.

Over seven covered days, assess all group-owned work whose milestone was missed,
including unselected cases. Report detected, investigated, actually resumed,
still unavailable, and false-positive interventions separately. Measure time
since last useful result, detection-to-action, action-to-resumption, and repeated
unchanged attempts. Acknowledgements, packets and restarted-agent counts are
not successful outcomes. Baseline is unknown until collected.

Guardrails: no duplicate/conflicting writes, lost acceptance/history, unrelated
process termination, weakened tests, or hidden unresolved work. Reviewer findings
should update this procedure only through the agreed configuration approval
path, and close only after a later recovery demonstrates the improvement.
