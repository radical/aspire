# CI-health prompt snapshots

These are complete prompt bodies, not excerpts or instructions to apply merely
by reading this directory. Metadata stays here so each body can be compared
byte-for-byte with the server. The server configuration remains authoritative.

Snapshot verified November 15, 2026, at approximately 06:05 UTC. All four latest
proposals were applied and all four automations remain enabled. The deployment
execution gate is enabled. Automatic GitHub events still report
`webhook_admin_required`; cron scheduling is separate.

| Role | Applied prompt | Generation | Active immutable revision |
| --- | --- | --- | --- |
| Monitor | [Current](current/monitor.md) | 24 | `db64382c-49dc-479a-9bb3-4b3995b230dd` |
| Fixer | [Current](current/worker.md) | 22 | `f36dd33d-4c41-434d-a92a-a80571d306e5` |
| Reviewer | [Current](current/reviewer.md) | 19 | `e6361b59-93dc-4284-9832-ae0040faccd2` |
| Supervisor | [Current](current/supervisor.md) | 17 | `bd247502-388e-4b76-95fe-917ba1e7c705` |

## Application evidence

Authoritative proposal comparisons returned `state=applied`,
`comparisonComplete=true`, and `changedFields=["prompt"]`. Complete before/after
values and server digests were checked against the saved files. The definition
listing confirmed the active generations and revision IDs above; the fixer's
full definition also matched its saved prompt digest.

| Role | Automation ID | Applied proposal | Activated UTC |
| --- | --- | --- | --- |
| Monitor | `5bd0ec02-21b4-4f5a-bf7a-41859226a236` | `8efbcdc7-6d0b-5e84-a93f-f3a3fb59993e` | 06:00:20 |
| Fixer | `9e82784d-ed3c-4188-b081-db683e2d3635` | `a983011e-270e-5e11-bbce-183526e190f2` | 06:03:10 |
| Reviewer | `62ff16a7-a74c-4c96-a23c-d053c3f35743` | `8cbee556-3fc0-5eb5-8557-47d69fabce4e` | 06:00:24 |
| Supervisor | `cbc3a8c3-743e-4d4e-a1de-cc3501ed419b` | `6ca2fa93-5c24-55ed-b4c6-171f6676cc0e` | 06:00:14 |

The fixer's initial unattended/readiness proposal,
`8d82ab8a-c5a1-526e-ba1e-4b44b9d968b5`, was applied at 05:57:40 as generation 21.
The later proposal adds the owner's backport preference and retains that policy.
It is not a duplicate pending request.

Two attempted updates were rejected by generation compare-and-swap, with no
effect. The monitor's concurrent change selected a different model without
changing its prompt; the fixer's concurrent change applied the initial proposal.
Both were reread and reconciled before submitting prompt-only updates. No model,
tool, schedule, enabled-state, environment, or permission field was submitted.

## Prompt sizes

The owner's authoring ceiling is 12,000 characters. These sizes were measured
from the exact files; byte counts are separate, not interchangeable limits.
The server accepted and applied all four bodies.

| Role | Characters | UTF-8 bytes |
| --- | --- | --- |
| Monitor | 11,980 | 11,980 |
| Fixer | 12,000 | 12,003 |
| Reviewer | 11,923 | 11,923 |
| Supervisor | 11,985 | 11,985 |

The fixer's retained bot-signature emoji explains its larger byte count.
These prompts have little room left. Consolidate policy and extract reusable
procedures before appending more instructions; do not remove safety requirements
merely to fit another rule.

## Unapplied 3-role candidates

`candidate/` holds complete, UNAPPLIED prompts for three roles: monitor,
fixer (`worker.md`) and supervisor. They are not live configuration;
`current/` remains the exact live snapshot. Each candidate is rendered from
`source/<role>.md` with the shared recovery core from
[`.agents/skills/automation-recovery/SKILL.md`](../../../../.agents/skills/automation-recovery/SKILL.md),
embedded inline because the skill exists only on this fork branch.

They replace the separate reviewer cron, at the owner's request:

- the fixer runs a fresh-context read-only review of each new owned PR head
  (native task tool if exposed, else `create_session` +
  `send_session_message` with an explicit reverse reply), persists it under
  `worker/reviews/<pr>/<head>/<reviewId>/` with a `worker/reviews/<pr>/current.json`
  latest ref (a fresh same-head review only after the prior one settles,
  never while unknown), consumes a native result immediately (the no-wait
  rule applies to child transport only), keeps at most one outstanding review
  per owned incident apart from the Windows diagnostic slot, and owns the
  ready gate;
- the monitor owns post-merge outcomes (`planner/outcomes/<pr>/<window>.json`)
  and failure-rate measurement;
- the supervisor verifies readiness handoffs, diagnoses one due blocked item
  per pass and takes a daily independently selected missed-work sample, with
  no audit-of-audit;
- legacy `verifier/*` records stay read-only evidence.

`create_session` has no model or tool parameter, so no child model is
promised and no child tool grant is assumed; the child's read-only limits are prompt instructions, not tool
enforcement. [prompt-obligations.md](../prompt-obligations.md) maps every
preserved obligation and every reviewer transfer.

The reviewed 4-role candidates (`9095dd85c`, never applied) are archived
unchanged under [archive-4role-9095dd85c/](archive-4role-9095dd85c/README.md)
and are NOT FOR ROLLOUT.

```bash
python3 docs/ci/automation-group/prompts/check_prompts.py --write       # render
python3 docs/ci/automation-group/prompts/check_prompts.py               # compare 3-role set
python3 docs/ci/automation-group/prompts/check_prompts.py --historical  # archived 4-role set
```

The checker verifies core placement, rendering, size and anchor presence
(obligation map plus reviewer-transfers table). It does not run or evaluate
an automation; passing is not behavioral evidence. Sizes and SHA-256 digests
are in [candidate/README.md](candidate/README.md).

## Deployment and reviewer retirement status

Status: UNAPPLIED, not yet independently reviewed. Deployment is BLOCKED by
hosted-tool failures: the 08:00 UTC client failure, a working read at 17:35,
then fresh `automation_get`/list calls aborted again at 18:32. No
`automation_upsert` or enable change was attempted for this set, and no
mutation outcome is unknown. Do not retry through other APIs or the shell.

Pending retirement sequence (nothing below has happened):

1. Apply the fixer, monitor and supervisor candidates: fresh `automation_get`
   per target, compare with `current/`, archive old bytes as
   `baseline-<UTC>/`, prompt-only CAS `automation_upsert` (`automation_id`,
   `repository_id` 696529789, `expected_generation`, `prompt`), then read back
   prompt and unchanged settings.
2. Observe one actual review result from a fixer review subagent, consumed
   under the new fixer revision (`worker/reviews/<pr>/<head>/<reviewId>/result.json`
   and `consumption.json`). No manual fault injection.
3. Only then disable reviewer `62ff16a7-a74c-4c96-a23c-d053c3f35743` with a
   fresh read and CAS `automation_set_enabled`; verify readback.
4. Old reviewer sessions stay frozen; their records remain read-only legacy
   evidence.

A pending proposal is not an applied change. Applied prompts affect future
runs only.

Rollback predecessors are the live snapshots in `current/`. Their generations are the
expected `expected_generation` values unless a fresh read shows drift. The
manager's `automation_get` read all four earlier on 2026-11-15 and the server
generation, revision and prompt digest matched these values; a local
`sha256sum` of each `current/` file matched independently. None was
revalidated at the attempted deployment: the immediate pre-deployment refresh
failed (monitor aborted, supervisor no active client), so a fresh read is still
required before any upsert.

| Role | Automation ID | Generation | Revision | `current/` SHA-256 |
| --- | --- | --- | --- | --- |
| Monitor | `5bd0ec02-21b4-4f5a-bf7a-41859226a236` | 24 | `db64382c-49dc-479a-9bb3-4b3995b230dd` | `8dec9d63d1402f1d90dcd10e900b834858715e96a48d555397e16aabfb599e35` |
| Fixer | `9e82784d-ed3c-4188-b081-db683e2d3635` | 22 | `f36dd33d-4c41-434d-a92a-a80571d306e5` | `9fa8c2f19691f31b8ed8b440223bbed53bda57557b9b218be02188556bd8629a` |
| Reviewer | `62ff16a7-a74c-4c96-a23c-d053c3f35743` | 19 | `e6361b59-93dc-4284-9832-ae0040faccd2` | `db2c93bd59c9dcde3df90e5a4e35213eed0133e09cfba2329c273581eb621c04` |
| Supervisor | `cbc3a8c3-743e-4d4e-a1de-cc3501ed419b` | 17 | `bd247502-388e-4b76-95fe-917ba1e7c705` | `2dac3d62ee838282cfd131a98ad1f901b0661016491c875812df3fcd635f43d0` |

## Earlier baseline

The complete approximately 02:52 UTC baseline is retained for comparison:

| Role | Earlier prompt | Generation | Immutable revision |
| --- | --- | --- | --- |
| Monitor | [Baseline](baseline-0252/monitor.md) | 22 | `72be3971-b5a1-4962-a608-c5e326bbcf36` |
| Fixer | [Baseline](baseline-0252/worker.md) | 20 | `f500579d-bd08-49b3-aae0-f915d2c11e02` |
| Reviewer | [Baseline](baseline-0252/reviewer.md) | 18 | `bff11ee0-cecd-434e-9fa3-5bf3f2de4b29` |
| Supervisor | [Baseline](baseline-0252/supervisor.md) | 16 | `dddeeb63-128b-4cf7-a204-3b0cb07eaca3` |

Configuration adoption is not task recovery. Previously admitted sessions keep
their frozen prompts and tools. An old interactive input remains unanswered
until an authorized continuation handles it; applying a prompt neither resumes
that session nor establishes the outcome of its commands.

Runtime IDs and repository references inside exact prompt bodies are retained
configuration, not process case histories. Keep evolving work state, logs,
claims, and outcomes in the authorized runtime ledger, not this directory.
