# Operating rules to carry into implementation

These rules combine explicit owner decisions and existing safety boundaries.
They describe intended behavior, not a claim that all current prompts, skills,
tools, or old sessions implement it.

## Autonomous routine decisions

1. Work from the accepted incident's source branch, run, attempt, and evidence.
   Target the fix PR at that branch. Do not ask whether to use main or
   release/17.0 when the source already answers it.
2. Correlate the same cause across branches without losing branch-specific
   validation and delivery. Do not silently retarget an existing PR or include
   an unapproved backport in the current task.
3. Resolve routine implementation, test-selection, and branch decisions using
   repository guidance and the agreed scope. Do not enter an interactive
   question loop for those decisions.
4. A real permission, cost, destructive-action, or scope boundary still needs
   authorization. Save the exact decision needed, route it to the responsible
   person, release execution resources safely, and work on another eligible
   item. Do not pretend the question was answered.
5. A failed question is an operational failure to handle, not a successful
   escalation. Record whether any recipient actually received the request.

## Use repository skills before inventing a procedure

Read the applicable skill at the start of the relevant operation. Record its
name and any justified deviation, not a copy of the whole skill in each report.

| Task | Existing skill |
| --- | --- |
| Investigate an issue | [issue-investigation](../../../.agents/skills/issue-investigation/SKILL.md) |
| Collect and diagnose CI failures | [ci-test-failures](../../../.agents/skills/ci-test-failures/SKILL.md) |
| Reproduce and fix intermittent tests | [fix-flaky-test](../../../.agents/skills/fix-flaky-test/SKILL.md) |
| VS Code extension diagnosis and testing | [vscode-extension](../../../.agents/skills/vscode-extension/SKILL.md) |
| Dashboard test changes | [dashboard-testing](../../../.agents/skills/dashboard-testing/SKILL.md) |
| CLI end-to-end tests | [cli-e2e-testing](../../../.agents/skills/cli-e2e-testing/SKILL.md) |
| Review a PR or local diff | [code-review](../../../.agents/skills/code-review/SKILL.md) |
| Open a PR | [create-pr](../../../.agents/skills/create-pr/SKILL.md) |
| Approved release backport | [backport-pr](../../../.agents/skills/backport-pr/SKILL.md) |

Skills explain how; they do not grant access, expand spending authority, or
override the automation's approved scope. Some general-purpose recipes include
actions the current group forbids, such as changing `ci.yml`, broad workflow
reruns, or creating additional investigation PRs. Preserve the narrower authority
and report the specific conflict rather than blindly executing the recipe.

Prefer existing evidence collectors and parsers over ad hoc log scraping.
Inspect reusable parts of [Analyze CI failures](../analyze-ci-failure.md) before
adding another collector. A read operation must not accidentally invoke its
publisher, issue creation, or rerun behavior.

## PR delivery

- Create contribution PRs as drafts, with the repository template and exact
  validation commands and results.
- Independently review the current head. Consume findings, repair the change,
  and re-review when needed. Keep agent-to-agent findings private.
- Once the agreed feedback and CI gates pass, actually mark the owned PR ready
  and verify GitHub reports it non-draft. Request appropriate human reviewers
  and verify those requests, without duplicating existing requests.
- Distinguish "eligible to mark ready" from "marked ready" and "awaiting human
  approval." Record who performed the transition when evidence establishes it.
- Human approval and merge remain human actions. Do not infer either from a
  passing agent verdict or green checks.

## Use precise meanings for recovery

| Term | Meaning | Evidence |
| --- | --- | --- |
| Resume interrupted work | Continue useful work after a session, handoff, tool, or environment problem | A specific next action actually executes and its result is used |
| Resolve an uncertain operation | Determine what an earlier command or external write did before repeating or overlapping it | Original operation evidence plus current target state |
| Restore CI health | Establish that an affected branch no longer exhibits the failure after the fix | Later eligible CI exercises the fix; observation window and coverage are stated |

Do not use the single word "recovery" for all three. A merged PR is not proof of
CI health, and an acknowledgement is not resumed work.

## Keep safety checks scoped to the affected action

- An unknown write or still-running command blocks conflicting writes, not all
  diagnosis of the incident.
- Permit independent reading, reproduction, and private validation in isolated
  workspaces within the existing authorization.
- Before modifying an original branch or workspace, verify the earlier
  executor's commands and effects are settled. Expiry or a completed turn alone
  does not prove this.
- Reconcile an uncertain request before issuing a replacement. Preserve the
  original identity and evidence; do not manufacture a new operation ID to evade
  uncertainty.
- A named recovery owner must correspond to a real person or session with a
  usable route to the required action. A field containing "platform owner" is
  not that route.
- Preserve original work and history during any state migration. Archive and
  verify old state before conditionally replacing current records.

## Windows routing

An online remote machine is an available destination, not the current execution
location. Explicitly select the intended environment for an authorized new
session or run. Verify actual OS, architecture, prerequisites, and working
directory there. Do not infer ARM64 support from "Windows."

Before reporting an environment blocker, check the applicable skill and permitted
alternatives, including the existing Windows .NET CI reproduction route. Describe
the exact unavailable action, not "Windows is blocked."

Do not globally move unrelated automation work to the owner's machine as a
shortcut. Shared-machine work must remain isolated: no broad cleanup, unrelated
process termination, global installations, or credential export.

## Human-readable status

Each status row should answer:

> What work? Who is doing it? What happened? What can happen next?
> Who must act, if anyone? Since when? What evidence supports this?

Use an automation name and a recognizable task/session label in the main view.
Keep exact IDs, revisions, ETags, and operation receipts in linked details.
Do not expose secrets or temporary signed URLs.

Bad:

> No accepted platform recovery action established.

Better:

> The original fixer has not provided evidence that its last command
> stopped. The new fixer is avoiding changes to the same branch. The supervisor
> recorded a request, but nobody with access to the original session has confirmed
> they will handle it. Independent diagnosis can continue.

Report scheduled, queued, executing, and completed times separately where known.
Never label admission-to-completion duration as execution or billing time.

## Learning without accumulating prose

Capture successful practices and problems in the persisted process notes:
observation window, evidence, consequence, proposed or applied remedy, responsible
role, and review date. Distinguish documented, implemented, and demonstrated
improvements. Repeated observations of the same mechanism update one finding
rather than creating a new request every pass. Keep specific issue/PR histories
and individual task state in the runtime ledger, not the process notes.

Turn recurring mechanical problems into tested helpers. Turn recurring procedural
mistakes into skill improvements. Keep only authority, objectives, and essential
invariants in standalone prompts. Every prompt retains its basic bounded-state
and uncertain-effect safeguards; a skill link cannot replace those guarantees.

Evaluate changes against actual resumed work, repaired defects, missed eligible
work, and later CI outcomes. Self-review text and run counts are not success
metrics.
