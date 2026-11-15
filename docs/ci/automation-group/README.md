# CI-health automation group: findings and operating design

These documents preserve the investigation and owner decisions from November 15,
2026. They concern the existing CI-health automation group for `microsoft/aspire`.
They are a design record, not a live dashboard or executable configuration.

| Document | Purpose |
| --- | --- |
| [Findings](findings.md) | Observed behavior, evidence, corrections, and unresolved questions |
| [Operating rules](operating-rules.md) | Agreed behavior that prompts, skills, and tooling should implement |
| [Proposed design](proposed-design.md) | A smaller decision loop, reusable procedures, state model, and implementation sequence |

## Current position

The group has a monitor, fixer, reviewer, and supervisor. Prompt changes applied
around 02:52 UTC on November 15 improved diagnosis and feedback handling, but did
not resolve every stalled task. The objective remains useful fixes and healthier
CI, not more automation runs, reports, or acknowledgements.

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
belongs in authorized durable storage. Link stable evidence from these documents
and date corrections rather than silently turning historical observations into
current claims.

## Reading the evidence

All times are UTC. Automation timing in the investigation generally came from
scheduled/admitted occurrences and recorded turn completion, not process-level
start/stop measurements. Those intervals can include queueing and must not be
reported as CPU time or billed usage.

The parallel audit inspected prompt files and the reproduction workflow. Its
hosted state reads failed, so it was not an independent verification of the latest
worker results. The manager separately read operational records and GitHub logs.
Neither inspection established a complete seven-day outcome baseline.
