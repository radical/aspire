---
name: automation-recovery
description: Recovery procedure for the CI-health automation group (monitor, fixer, reviewer, supervisor) when owned work is blocked, quiet, interrupted, waiting for input, or has an uncertain external effect. Use when deciding whether work is progressing, classifying a missed milestone, reconciling a lost reply, parking an unchanged blocker, or authoring the shared recovery core of a CI-health prompt. Grants no tools, permissions, or authority.
---

# CI-health automation recovery

This skill explains the recovery rules shared by the four CI-health automation
roles. It is a procedure, not a grant: it adds no tools, permissions, models,
schedules, environments, cancellation authority, or replacement executors.

The skill lives in the documentation fork and is **not** deployed with the
upstream automations. Each complete automation prompt therefore embeds the
canonical core below verbatim. Running automations must never depend on
fetching this file. Edit the core here, re-render the prompts with
`docs/ci/automation-group/prompts/check_prompts.py --write`, and re-check them.

## Canonical recovery core

The checker copies the text inside this fenced block into every prompt where
`{{RECOVERY_CORE}}` appears. Keep it ASCII and short; every character is paid
for in all four prompts.

<!-- recovery-core:begin -->
```text
RECOVERY CORE v1 (all ci-health roles; grants nothing):
1 Progress=new evidence (phase/result/narrowed cause/verified effect), not heartbeats/renewals/logs/retries/rereads; lastUsefulProgressAt=evidence time.
2 Reuse actionId/firstBlockedAt/lastUsefulProgressAt/expectedBy+basis/nextCheckAt/attempts/effects/escalation; optional milestone/reopenCondition/blockedScope/actionStatus/operationRefs; <=3 attempts inline, rest immutable history. Never reset ages/deadlines/IDs.
3 Missed milestone: classify queue/execution/input/result publication/consumption. Age prompts diagnosis, never kill/takeover/duplicate.
4 Lost reply: authoritative state matching the exact intended result settles outcome, not executor stop; no blind replay.
5 Routine question: standing policy; old waiting session resolved only by authorized input route plus delivery/ack evidence.
6 Two eligible unchanged checks: different supported action or ONE capability-gap record (operation/error/owner action).
7 Parked (blockedScope+observable reopenCondition): no costly probes/new escalation; one cheap reopen check, concise visibility, no timestamp/ID churn.
8 Unknown effects block overlapping mutation only; independent work continues.
9 Ending turn/releasing claim keeps delivery duty; expiry never proves termination.
10 Denied/missing tools: capability evidence, not task state or workaround license.
```
<!-- recovery-core:end -->

## Decision table

Classify from exact evidence before acting. "Due" means `nextCheckAt` or
`expectedBy` has passed and the action is not parked.

| Observation | Classification | Next supported action | Never |
| --- | --- | --- | --- |
| New phase/result/checkpoint since last check, even with quiet logs | Progressing | Record evidence time as `lastUsefulProgressAt`; set the next milestone with a stated basis | Treat silence as a hang |
| Logs, renewals or "still investigating" but no new result | Non-progress | Keep the old `lastUsefulProgressAt`; diagnose on the first missed milestone | Count activity as progress |
| Work not admitted; capacity or schedule delay | Queue/admission | Record queue evidence; wait for admission or the next eligible pass | Start a duplicate or call it a hang |
| Admitted command running past its milestone | Execution | Inspect that exact command/dependency with authorized tools; record the result | Kill, take over, or replace |
| Session waits on a routine question | Input | Apply standing policy; deliver only through an available authorized input route and record delivery/ack | Re-ask the owner; claim resolution without delivery evidence |
| Result exists but no record or receipt | Result publication | Settle from authoritative state matching the exact intended result | Replay the operation |
| Result recorded but consumer has not used it | Consumption | Route the exact ref to the consumer; check its next eligible pass | Regenerate the result |
| Two eligible checks, nothing changed | Unchanged | Take a different supported action, or write one capability-gap record, then park | Rename and resend the same request |
| Parked, reopen condition unchanged | Parked | One cheap observable check; concise visibility | Probe expensively, re-escalate, bump timestamps |
| Parked, reopen condition changed | Reopened | Clear parked status; resume due handling with the same `actionId` | Create a new incident or action |
| Effect of an earlier mutation unknown | Uncertain effect | Block only overlapping mutation in `blockedScope`; inspect the target | Block unrelated work; blind retry |
| Another holder's lease is held and unexpired | Concurrency | No protected writes, no waiting or polling; end (the fixer skips only that incident) | Wait, take over, or write under the other lease |
| Two current-head verdicts conflict | Conflicting verdict | Resolve from evidence or publish a superseding verdict naming the replaced one | Treat either as a pass; push a new head only to correct it |
| Tool denied, missing or unregistered | Capability gap | Record exact operation, error and owner action once | Infer task state; use another identity or route |

## State guidance

Reuse existing fields: `actionId`, `firstBlockedAt`, `lastUsefulProgressAt`,
`expectedBy`, `nextCheckAt`, `attempts`, `effects`, `escalation`. The only
additions are optional: `milestone`, `reopenCondition`, `blockedScope`,
`actionStatus` (for example `active`, `external-wait`, `parked`, `resolved`),
and `operationRefs`. Records without them remain valid; there is no migration.

- Keep hot records plus a transition reserve of at least 4096 bytes within
  32768 bytes. Keep at most three recent attempts inline; move older ones to
  create-only immutable history and reference them.
- `expectedBy` states its basis (documented timeout, CI cadence, consumer
  schedule). It triggers diagnosis; it is never kill or takeover authority.
- A parked action keeps its identity and ages. Do not rewrite `nextCheckAt`
  or other timestamps on every pass merely to show a check happened; record a
  check only when its result differs or visibility needs a dated summary.
- `blockedScope` names the exact resources an unknown effect can touch, such as
  one PR branch or one artifact. Work outside that scope proceeds.
- Claims keep `settle-expire-v1`: expiry or release alone never proves the
  original executor stopped or its effects settled.

## Worked examples

1. **Quiet progress.** A Windows build logs nothing for 20 minutes, then the
   next check finds a newly completed restore and a test-phase summary. That is
   progress: stamp the summary time, set the next milestone from the test
   timeout, and do not intervene.
2. **Noisy non-progress.** A worker renews its claim every five minutes and logs
   "still investigating" with identical stack traces. `lastUsefulProgressAt`
   stays at the last new finding; the missed milestone is classified as
   execution non-progress and diagnosed.
3. **Queue delay.** A due fixer pass has not been admitted because the schedule
   has not fired. Classify queue/admission, not a stalled worker. Do not start
   another session.
4. **Duplicate observations.** Monitor, reviewer and supervisor all see the same
   overdue action. Each references the existing `actionId`; the reviewer keeps
   its immutable finding until acknowledged rather than publishing a new one.
5. **Lost successful effect.** A push reply is lost. The branch head equals the
   intended commit, so the push outcome is settled from the branch. That does
   not prove the original session stopped; no successor mutates the branch
   until claim settlement is verified.
6. **Stopped worker with pending external effects.** The worker's turn ended
   after dispatching a reproduction workflow. The dispatch is reconciled from the
   run record before any successor dispatch; ending the turn did not hand off
   delivery responsibility.
7. **Expired lease.** A claim expired with no settled checkpoint. Expiry is not
   termination. No takeover; the supervisor records the settlement evidence
   still needed and the worker continues independent diagnosis elsewhere.
8. **Unavailable input or inspection.** An old session waits for a branch choice
   already answered by policy, and no authorized input route exists. Record the
   missing route once as a capability gap and park with the reopen condition
   "input route becomes available"; do not keep asking the owner.
9. **Parked reopen.** A parked action awaits Windows environment access. Each
   pass makes one cheap availability check. When that check reports matching
   OS/architecture readiness, the action reopens with the same `actionId`.
10. **Changed heads or concurrency.** A CAS write conflicts because another role
    changed the record, or the PR head moved. Reread, reconcile, and re-derive
    the action; never substitute ETags or overwrite another writer's fields.
11. **Independent work.** An unknown PR-comment write blocks further comments on
    that PR only. The worker still diagnoses another incident and validates
    privately in an isolated workspace.

12. **Lease held elsewhere.** The reviewer finds `verifier/lease.json` held by
    another unexpired holder. It makes no protected writes and ends without
    polling. Current pointers only point to records; they are not a full
    inventory, so the next holder still lists the records.

## Role hooks

- **Monitor:** show actor, last useful result, concrete wait reason, parked
  reopen condition and next action in plain language.
- **Fixer:** record milestones and operation refs before effects; keep delivery
  responsibility across turns; settle its own uncertain effects.
- **Reviewer:** sample missed work and actual outcomes; keep existing finding
  identities; audit parked reasons rather than re-escalating them; publish only under the
  required verifier lease (older sessions without it remain noncompliant).
- **Supervisor:** classify missed milestones, choose a different supported
  action or one capability-gap record, and never write worker-owned claims or
  lifecycle fields.

## Limits

This skill cannot inspect, stop, resume, or replace a remote session. The
catalog's original-session reconnect, where selected, only reconciles work that
is already idle or settled. Missing or denied controls are capability gaps to
report, not authority to improvise another route.

Artifact CAS is not atomic with GitHub side effects, and prompt bounds are not
runtime-enforced caps. These rules reduce duplicate effects; they cannot
guarantee their absence. There is no watchdog, notification route or shared
backoff ledger.
