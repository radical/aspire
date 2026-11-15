# Process findings and improvement log

This is a learning record about the automation process: coordination, execution,
tracking, reporting, and improvement. It is not an inventory of Aspire defects,
issues, PRs, or individual worker tasks. Those details belong in the runtime work
ledger and its evidence records.

Initial observations: November 14-15, 2026. The post-update audit covered starts
from 02:57 through 04:30 on November 15, with selected follow-up reads through
05:29. Entries below describe that window unless explicitly dated otherwise.

## What is working well

| Practice | What we observed | Why it helps / what to preserve |
| --- | --- | --- |
| Independent review with a feedback path | Reviewer critiques reached the supervisor and fixer; later records showed some critiques consumed and reporting errors corrected | Keep independent review, but check that feedback changes an action rather than merely being acknowledged |
| Diagnosis without taking over another executor's work | A fixer performed source-only diagnosis while overlapping changes remained blocked | Keep investigation available; apply exclusivity only to conflicting actions |
| Reusing an existing execution procedure | A fixer found and used the repository's permitted missing-OS CI reproduction route | Skills and existing workflows can remove a supposed blocker without another automation |
| Checking actual output rather than a green status | The manager read executed-test totals and distinguished a successful diagnostic run from reproducing or fixing a failure | Preserve evidence-based completion criteria |
| Checkpointing before external waits | The inspected diagnostic pass recorded its external run, settled local commands, and produced a claim-release receipt | A worker can stop occupying execution resources without forgetting the work |
| Incremental baseline collection | The monitor advanced its historical collection cursor across four consecutive passes | Bounded backfill can progress alongside urgent work; it is measurement, not repair |
| Explicit uncertainty | Reports retained unknown outcomes, incomplete coverage, and the difference between configuration adoption and effective execution | Keep unknowns visible without turning every unknown into an incident-wide stop |

These are observed examples, not evidence that every pass behaves this way.
No measured reduction in billing, CPU use, or overall failure rate is established.

## Problems found and how we are addressing them

### Routine decisions became human-input waits

**Observed:** a worker was waiting for user input, and the owner supplied an
example of a routine branch-selection question. The complete question history
could not be retrieved, so its exact causal sequence is not established.

**Mechanism:** the automation failed to apply a decision already available from
source evidence or policy, then depended on an interactive response path.

**Change:** document source-branch targeting and autonomous routine decisions in
[operating rules](operating-rules.md). Genuine authorization boundaries still
require a specific decision, but should not hold up unrelated work.

**Status, November 15, 06:05 UTC:** unattended operation and deterministic
source-branch/`main` fallback rules are applied to all four role prompts.
Resolution of already-waiting tasks and consistent future behavior remain
unverified.

### Release fixes can duplicate work already completed on main

**Observed:** a later source investigation identified an existing main-branch
remedy missing from a release branch. The owner explicitly prefers reuse.

**Mechanism:** starting implementation from the release failure alone can miss
an established fix and create an unnecessary independent solution.

**Change:** the fixer searches main and existing source/target backports first,
prefers an applicable merged fix, follows `backport-pr` checks and shiproom
formatting, and validates on the target branch. Monitor supplies leads; reviewer
checks reuse and target validation. Ownership and existing grants still apply.

**Status, November 15, 06:05 UTC:** applied to the relevant prompts. A completed
backport using the new instruction has not yet been demonstrated.

### Handoffs were recorded without a reachable executor

**Observed:** the supervisor repeatedly retained requests for help with original
sessions, without evidence that a person or session capable of acting received
and accepted them. Public report-body updates did not establish delivery.

**Mechanism:** naming an owner in state was treated as a substitute for a working
communication and execution path. The manager did not close that gap.

**Change:** keep one stable request with the exact required action, intended
recipient, delivery result, and acceptance state. If no route exists, report that
missing route plainly. Repeatedly rewriting the request is not progress.

**Status:** unresolved. The proposed design explains the remedy, but no general
working original-session recovery path has been demonstrated.

### Action-specific safety checks became broad blockers

**Observed:** original-command uncertainty and missing access to one environment
were described as reasons that an entire incident could not proceed.

**Mechanism:** "cannot modify this branch safely" became "cannot investigate,"
and "cannot create a child on that machine" became "cannot run on Windows."

**Change:** show the blocked action and the independent actions still available.
Check applicable skills and authorized alternative execution routes first.
Preserve checks against overlapping writes and duplicate uncertain operations.

**Status:** current prompts already permit isolated diagnosis; a permitted CI
fallback executed. Consistent application and clearer reporting remain incomplete.

### Environment availability was confused with work placement

**Observed:** an offered remote machine was online with no reported group
sessions. The manager and the fixer's earlier reports described different tool
capabilities. The fixer had no saved default environment.

**Mechanism:** machine connection, session routing, runtime capability, and task
readiness were collapsed into one "access" status.

**Change:** track these separately. Demonstrate one explicitly routed diagnostic
and returned result before declaring the path usable or changing machine setup.
Do not move unrelated work onto the owner's machine.

**Status:** machine availability verified at the stated read; local-machine
diagnostic routing and result delivery not verified.

### Readiness reporting did not establish who performed the handoff

**Observed:** GitHub state and timeline verified a non-draft PR and reviewer
requests, but did not establish that the fixer itself performed the transition.

**Mechanism:** "eligible for review," "marked ready," "review requested," and
"human approved" were easy to conflate. Observing state is not causing it.

**Change:** make the fixer perform authorized readiness actions and verify them.
Record operation evidence when attributing the action to an automation; otherwise
report only the observed external state.

**Status, November 15, 06:05 UTC:** the fixer now has an applied instruction to
perform and verify the ready transition and reviewer requests once its gates
pass; reviewer checks the actual state. End-to-end execution still needs
verification.

### Routine reporting generated too much coordination work

**Observed:** the monitor prompt required a public body update, intent,
precondition checks, verification, and receipt for every completed assessment.
Large state reads and repeated decoding also increased report-construction work.

**Mechanism:** freshness-only bookkeeping took the same expensive path as a
material change. Agents repeatedly handled mechanical serialization and rendering.

**Change:** publish material changes plus a bounded freshness heartbeat. Use small
current records and reusable validation/rendering helpers, retaining required
conditional writes and verification.

**Status:** proposed, not implemented. The overhead is observable, but its share
of execution time and the potential savings have not been measured.

### Scheduling delay could look like ineffective execution

**Observed:** monitor occurrences shared one session and queued behind previous
turns. Admission-to-completion spans grew even though the queue later advanced.

**Mechanism:** a single duration or deadline cannot distinguish waiting for
delivery, active work, acknowledgement, and external-result consumption.

**Change:** preserve separate timestamps and wall-clock blocked age. Diagnose
which stage missed its milestone before blaming the consumer or adding workers.

**Status:** corrected in the manager's timeline explanation; a shared renderer
and consistent record fields remain proposed.

### Measurement and self-review could substitute for useful outcomes

**Observed:** baseline collection advanced, while some present-day blockers
remained unchanged. Several acknowledgements were recorded before any resulting
action was demonstrated.

**Mechanism:** collection counts, self-review prose, and handoff receipts are
easier to produce than evidence of resumed work or improved CI.

**Change:** separately measure collection coverage, useful next actions, result
consumption, delivery transitions, and later CI outcomes. Include unselected due
work in the denominator. Close a process finding only on evidence that its remedy
worked, not when someone proposed or acknowledged it.

**Status:** distinctions are documented and used in the audit; complete outcome
measurement remains pending.

### The audit itself spent too long gathering evidence

**Observed:** the manager decoded a large state dump before extracting relevant
fields. A parallel audit exceeded its requested duration and could not read
hosted state, so its findings relied partly on supplied operational evidence.

**Mechanism:** broad retrieval preceded a bounded question, and the evidence
available to the independent reviewer was not confirmed early.

**Change:** start with a small status snapshot, test required read access early,
and investigate only the unresolved question. Return useful partial findings
promptly when access prevents independent verification.

**Status:** limitation disclosed; procedure improvement proposed, not yet measured.

## Changes made and evidence still needed

| Date (UTC) | Change | What was actually completed | Follow-up evidence needed |
| --- | --- | --- | --- |
| November 15, around 02:52 | Applied recovery-loop prompt revisions | Saved configuration changed for the four existing automations | Consistent action on due work across later runs |
| November 15, 02:57-04:35 inspection window | Observed partial adoption | Independent diagnosis, feedback consumption, a permitted diagnostic execution, and baseline cursor progress | End-to-end result use, unresolved handoff resolution, and later CI health |
| November 15, documentation pass | Captured rules and simpler operating design | Versioned notes distinguish observations, owner decisions, and proposals | Implemented helpers/procedures and verified configuration changes |
| November 15, process-notes revision | Refocused persisted findings on operating mechanisms | Added working practices, problem mechanisms, remedy status, and follow-up checks; removed task-specific case histories | Keep subsequent updates equally process-focused |
| November 15, 06:00-06:03 | Applied unattended, skill-use, branch-selection, readiness, and backport rules | Four prompt-only updates applied; complete current and baseline snapshots verified and saved | New runs use the rules, routine input waits disappear, and release fixes reuse main when appropriate |

Do not advance a row from "documented" to "implemented" or "effective" without
new evidence. Editing these notes changes neither running sessions nor saved
automation configuration.

## How to maintain this log

For each recurring process lesson, retain: observation window, what happened,
why it matters, proposed or applied change, current status, responsible role,
and evidence needed to judge the result. Identify the responsible role in the
remedy or follow-up; do not invent an accepted executor.

Record successful practices as well as failures. Revisit a working practice if
it regresses. Update an existing mechanism rather than adding a new entry for
every incident exhibiting it. Preserve dated conclusions when later evidence
changes the assessment.

Keep issue numbers, PR histories, run IDs, raw logs, claim receipts, and detailed
task status in the work ledger. These notes should explain how the system learns
to operate better, without requiring a reader to understand a particular
repository defect.
