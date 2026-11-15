---
name: automation-recovery
description: Recovery procedure for the CI-health automation group (monitor, fixer, supervisor; legacy reviewer) when owned work is blocked, quiet, interrupted, waiting for input, or has an uncertain external effect. Use when deciding whether work is progressing, classifying a missed milestone, reconciling a lost reply, parking an unchanged blocker, or authoring the shared recovery core of a CI-health prompt. Grants no tools, permissions, or authority.
---

# CI-health automation recovery

This skill explains the recovery rules shared by the CI-health automation
roles (the 3-role candidates; the reviewer cron is legacy, pending retirement). It is a procedure, not a grant: it adds no tools, permissions, models,
schedules, environments, cancellation authority, or replacement executors.

The skill lives in the documentation fork and is **not** deployed with the
upstream automations. Each complete automation prompt therefore embeds the
canonical core below verbatim. Running automations must never depend on
fetching this file. Edit the core here, re-render the prompts with
`docs/ci/automation-group/prompts/check_prompts.py --write`, and re-check them.

## Canonical recovery core

The checker copies the text inside this fenced block into every prompt where
`{{RECOVERY_CORE}}` appears. Keep it ASCII and short; every character is paid
for in every prompt (three candidates, four archived).

<!-- recovery-core:begin -->
```text
RECOVERY CORE v2 (grants nothing):
1 Progress=new evidence (phase/result/narrowed cause/verified effect), not heartbeats/renewals/logs/retries/rereads; lastUsefulProgressAt=evidence time.
2 Reuse actionId/firstBlockedAt/lastUsefulProgressAt/expectedBy+basis/nextCheckAt/attempts/effects/escalation; optional milestone/reopenCondition/blockedScope/actionStatus/operationRefs; <=3 attempts inline, rest immutable history. Never reset ages/deadlines/IDs.
3 Due=nextCheckAt/expectedBy reached (undated legacy: now). Not due/stalled: evidenced waits before due; parked until reopenCondition verifiably changes. Missed milestone: classify queue/execution/input/result publication/consumption. Age prompts diagnosis, never kill/takeover/duplicate.
4 Lost reply: authoritative state matching the exact intended result settles that operation's outcome only (not executor stop/claim settlement/adoption); no blind replay.
5 Routine question: standing policy; old waiting session resolved only by authorized input route+delivery/ack evidence.
6 Two eligible unchanged checks (parked excluded): different supported action or ONE capability-gap record (operation/error/owner action).
7 Parked=action metadata (blockedScope+observable reopenCondition), not item/work state: no costly probes/new escalation; one cheap reopen check, concise visibility, no timestamp churn.
8 Unknown effects (not lease contention) block overlapping mutation only; independent work continues. Keep original call/operation/args/bytes/ETags; inspect target first. CAS conflict: reconcile, never substitute ETags.
9 Ending turn/releasing claim keeps delivery duty; expiry never proves termination.
10 Denied/missing tools: capability evidence, not task state or workaround license.
11 New prompts bind new runs only; old sessions keep frozen rules (adoption gap, not refusal). Absent optional fields=legacy, not false/resolved/awaited.
```
<!-- recovery-core:end -->

## Decision table

Classify from exact evidence before acting. "Due" means `nextCheckAt` or
`expectedBy` has passed and the action is not parked. Undated legacy blockers
are due now; their timestamps are backfilled additively.

| Observation | Classification | Next supported action | Never |
| --- | --- | --- | --- |
| New phase/result/checkpoint since last check, even with quiet logs | Progressing | Record evidence time as `lastUsefulProgressAt`; set the next milestone with a stated basis | Treat silence as a hang |
| Logs, renewals or "still investigating" but no new result | Non-progress | Keep the old `lastUsefulProgressAt`; diagnose on the first missed milestone | Count activity as progress |
| Work not admitted; capacity or schedule delay | Queue/admission | Record queue evidence; wait for admission or the next eligible pass | Start a duplicate or call it a hang |
| Admitted command running past its milestone | Execution | Inspect that exact command/dependency with authorized tools; record the result | Kill, take over, or replace |
| Session waits on a routine question | Input | Apply standing policy; deliver only through an available authorized input route and record delivery/ack | Re-ask the owner; claim resolution without delivery evidence |
| Result exists but no record or receipt | Result publication | Settle that operation's outcome from authoritative state matching the exact intended result | Replay the operation; treat it as executor stop or claim settlement |
| Result recorded but consumer has not used it | Consumption | Route the exact ref to the consumer; check its next eligible pass | Regenerate the result |
| Two eligible unparked checks, nothing changed | Unchanged | Take a different supported action, or write one capability-gap record, then park | Rename and resend the same request |
| Parked, reopen condition unchanged | Parked | One cheap observable check; concise visibility | Probe expensively, re-escalate, bump timestamps |
| Parked, reopen condition changed | Reopened | Clear parked status; resume due handling with the same `actionId` | Create a new incident or action |
| Effect of an earlier mutation unknown | Uncertain effect | Block only overlapping mutation in `blockedScope`; inspect the target | Block unrelated work; blind retry |
| Another holder's lease is held and unexpired | Concurrency (contention, not an unknown outcome) | No protected writes, no waiting or polling; end (the fixer skips only that incident) | Wait, take over, write under the other lease, or open an uncertainty record |
| Legacy (archived 4-role) fenced reviewer write returns `fence_lost` | Lost exclusion | Stop protected writes for this pass; reconcile on a later pass under a valid lease | Substitute another ETag; retry blindly |
| Legacy (archived 4-role) reviewer `artifact_put` has no `fence` | Capability gap | Keep publication blocked; record the exact gap once (configured selection is not runtime attestation) | Publish unfenced as a fallback |
| Older session published an unfenced or lease-less verdict | Mixed generation | Reconcile it as read-only evidence; resolve or request a fresh exact-head review | Ignore a blocking verdict; treat last writer as pass |
| Two current-head verdicts conflict | Conflicting verdict | Resolve from evidence or a fresh exact-head review | Treat either as a pass; push a new head only to correct it |
| Tool denied, missing or unregistered | Capability gap | Record exact operation, error and owner action once | Infer task state; use another identity or route |
| Record lacks a new optional field | Legacy record | Read it as legacy; backfill additively when owned | Treat absence as false or resolved; wait for the field |

## State guidance

Reuse existing fields: `actionId`, `firstBlockedAt`, `lastUsefulProgressAt`,
`expectedBy`, `nextCheckAt`, `attempts`, `effects`, `escalation`. The only
additions are optional: `milestone`, `reopenCondition`, `blockedScope`,
`actionStatus` (for example `active`, `external-wait`, `parked`, `resolved`),
and `operationRefs`. Records without them remain valid; there is no migration.
Parked is action metadata only. Top-level item and work state enums do not
change, and readers must tolerate the absence of every optional field.

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
  original executor stopped or its effects settled. Each role keeps its own
  exact settlement gate; outcome reconciliation never substitutes for it.
- New prompts bind only runs that start after deployment. Running sessions keep
  the rules they started with; that is an adoption gap, not a refusal, and
  nothing drains or stops them.

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
4. **Duplicate observations.** Monitor and supervisor both see the same
   overdue action. Each references the existing `actionId`; the supervisor keeps
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

Examples 12–14 are HISTORICAL (archived 4-role reviewer); the 3-role set
writes no `verifier/*` records.

12. **Lease held elsewhere.** The reviewer finds `verifier/lease.json` held by
    another unexpired holder. It makes no protected writes and ends without
    polling. Current pointers only point to records; they are not a full
    inventory, so the next holder still lists the records.
13. **Fence lost.** The reviewer renews its lease (the ETag changes), then a
    publication carrying the old ETag returns `fence_lost` with no effect. It
    stops protected writes for this pass and does not retry with a guessed
    ETag. A later pass reconciles under a valid lease.
14. **Mixed generations.** An older reviewer session publishes an unfenced
    `changes-needed` verdict for the current head while a newer one passed.
    Neither is treated as a pass; the current holder resolves the evidence or
    publishes a superseding verdict that names the replaced one.

## Role hooks

- **Monitor:** own post-merge outcomes and failure rates; show actor, last useful result, concrete wait reason, parked
  reopen condition and next action in plain language.
- **Fixer:** record milestones and operation refs before effects; keep delivery
  responsibility across turns; settle its own uncertain effects.
- **Fixer review child:** at most one outstanding per owned incident; an
  unknown or timed-out create/send holds the slot (no duplicate or
  replacement); accept only an actual result or explicit reply, then recheck
  the head.
- **Legacy reviewer:** pending retirement; its `verifier/*` records are
  read-only evidence and old blocking feedback cannot vanish.
- **Supervisor:** verify readiness handoffs, one due blocked item per pass and
  a daily missed-work sample; classify missed milestones, choose a different supported
  action or one capability-gap record, and never write worker-owned claims or
  lifecycle fields.

## Limits

This skill cannot inspect, stop, resume, or replace a remote session. The
catalog's original-session reconnect, where selected, only reconciles work that
is already idle or settled. Missing or denied controls are capability gaps to
report, not authority to improvise another route.

Artifact CAS and the `artifact_put` fence are atomic only for artifact writes,
not for GitHub side effects, and prompt bounds are not runtime-enforced caps. These rules reduce duplicate effects; they cannot
guarantee their absence. There is no watchdog, notification route or shared
backoff ledger.
