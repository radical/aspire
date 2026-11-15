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

## Unapplied recovery candidates

`candidate/` holds complete, UNAPPLIED candidate prompts for the four roles.
They are not live configuration. `current/` remains the exact snapshot of the
live prompts. Each candidate is rendered from `source/<role>.md` by inserting
the single shared recovery core from
[`.agents/skills/automation-recovery/SKILL.md`](../../../../.agents/skills/automation-recovery/SKILL.md).
The skill exists only on this fork branch and upstream runs cannot fetch it,
so every candidate embeds the core inline and does not refer to the skill.

The candidates:

- replace the contradictory recovery paragraphs with the core and short role
  hooks;
- add a rule for a lease held by another holder: no protected writes and no
  waiting (the fixer skips only that incident);
- make the reviewer's verifier lease required;
- state that a conflicting current-head verdict is not a pass;
- state that current pointers are not a full inventory.

[prompt-obligations.md](../prompt-obligations.md) maps each preserved
obligation to anchor text and lists the accepted compressions.

Rebuild and check the candidates:

```bash
python3 docs/ci/automation-group/prompts/check_prompts.py --write  # render
python3 docs/ci/automation-group/prompts/check_prompts.py          # compare only
```

The checker:

- extracts the core;
- verifies each rendered prompt contains it exactly once;
- compares the rendered prompts with `candidate/`;
- reports code points, UTF-16 units and UTF-8 bytes against the 11,400
  authoring target and the 12,000 ceiling;
- checks that every mapped anchor is present.

It does not run, simulate or evaluate an automation. A passing check is not
behavioral evidence.

Latest checker result (exit 0, 49 obligation rows, 223 anchors, RECOVERY CORE
v2 at 1,852 characters). All four prompts exceed the 11,400 target because the
standalone obligations were restored in full; the target is advisory and every
prompt stays under the 12,000 ceiling:

| Role | Code points | UTF-16 units | UTF-8 bytes |
| --- | --- | --- | --- |
| Monitor | 11,846 | 11,846 | 11,846 |
| Fixer | 11,973 | 11,974 | 11,976 |
| Reviewer | 11,971 | 11,971 | 11,971 |
| Supervisor | 11,861 | 11,861 | 11,861 |

The manager owns deployment. The steps:

1. Read each target fresh with `automation_get`.
2. Compare its prompt with `current/<role>.md`, and reconcile any drift
   instead of overwriting it.
3. Archive the authoritative old bytes as `baseline-<UTC>/`.
4. Send a prompt-only `automation_upsert` with `automation_id`,
   `repository_id` 696529789, `expected_generation` and `prompt`.
5. Read back both the prompt and all unchanged settings.

A result may come back as a pending proposal instead of an applied change.
Until the readback is verified, these files stay candidates. Applied prompts
affect future runs only; existing sessions keep their frozen prompts.

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
