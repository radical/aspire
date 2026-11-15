# CI-health automation group: findings and operating design

These documents preserve the investigation and owner decisions from November 15,
2026. They concern the existing CI-health automation group for `microsoft/aspire`.
They are a process-learning and design record, not a live dashboard or executable
configuration. Focus on what works, coordination and tracking problems, changes
made, and whether those changes helped. Specific repository issue/PR histories
belong in the runtime work ledger, not these notes.

| Document | Purpose |
| --- | --- |
| [Process findings](findings.md) | Working practices, recurring problems, remedy status, and follow-up evidence |
| [Operating rules](operating-rules.md) | Agreed behavior that prompts, skills, and tooling should implement |
| [Proposed design](proposed-design.md) | A smaller decision loop, reusable procedures, state model, and implementation sequence |
| [Complete prompt snapshots](prompts/README.md) | Applied prompts, earlier baseline, exact revisions, proposal receipts, and adoption limits |
| [Recovery playbook](recovery-playbook.md) | Proposed progress checks, interrupted-command recovery, stuck-agent handling, and required runtime capabilities |
| [Automation recovery skill](../../../.agents/skills/automation-recovery/SKILL.md) | Canonical recovery core, decision table, and worked examples (fork-only; embedded inline in prompts) |
| [Prompt obligation map](prompt-obligations.md) | Where each live-prompt obligation survives in the unapplied candidates, and accepted compressions |

## Current position

The group has a monitor, fixer, reviewer, and supervisor. UNAPPLIED 3-role
candidates fold review into a fixer child and retire the reviewer only after a
proven replacement (see [prompts/README.md](prompts/README.md)). Prompt changes applied
around 02:52 UTC on November 15 improved diagnosis and feedback handling, but did
not resolve every stalled task. The objective remains useful fixes and healthier
CI, not more automation runs, reports, or acknowledgements.

At 06:00-06:03 UTC, further prompt-only updates were applied to all four roles:
unattended routine decisions, source-branch targeting with a `main` fallback,
explicit repository-skill use, verified readiness actions, and release-fix reuse
from `main` with backport shiproom formatting. Existing sessions are not upgraded
by configuration edits. The [snapshot index](prompts/README.md) records exactly
what was applied; effective recovery still requires later execution evidence.

The immediate lesson is not "add another rule for every failure." Keep explicit
policy in short prompts, reusable procedures in repository skills, and mechanical
bookkeeping in tested tools. Preserve real safety checks while removing decisions
that the source evidence or an existing owner policy already answers.

## Status and authority

- **Verified observation:** backed by a stated tool response, GitHub event, log,
  or inspected configuration.
- **Historical observation:** describes the stated inspection window, not current
  live state.
- **Owner rule:** an explicit decision to carry into implementation.
- **Proposal:** not yet an applied configuration or implemented capability.
- **Unknown:** missing evidence; neither success nor failure.

The owner authorized publishing these notes on a new branch in `radical/aspire`.
This documentation change does not alter automation configuration, grant new
permissions, start a worker, answer an old task's pending input, or deploy a skill.
The newer operating rules below are not automatically available to existing runs.

Do not use this directory as the frequently rewritten work ledger. Runtime state
belongs in authorized durable storage. Preserve the observation window and
evidence basis for process lessons, and date corrections rather than silently
turning historical observations into current claims. Distinguish a proposed fix,
an applied change, and an observed improvement.

## Reading the evidence

All times are UTC. Automation timing in the investigation generally came from
scheduled/admitted occurrences and recorded turn completion, not process-level
start/stop measurements. Those intervals can include queueing and must not be
reported as CPU time or billed usage.

The parallel audit inspected prompt files and the reproduction workflow. Its
hosted state reads failed, so it was not an independent verification of the latest
worker results. The manager separately read operational records and GitHub logs.
Neither inspection established a complete seven-day outcome baseline.
