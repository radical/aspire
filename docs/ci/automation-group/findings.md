# Findings and corrections

Observation dates: November 14-15, 2026. Later changes may supersede these facts.
The post-update activity audit covered automation starts from 02:57 through 04:30
on November 15, with selected follow-up reads through 05:29.

## 1. PR readiness needs an action and evidence, not a description

[PR #22286](https://github.com/microsoft/aspire/pull/22286) was reported as ready.
A fresh GitHub read around 05:28 on November 15 returned:

- `isDraft: false`, base `release/17.0`, and `reviewDecision: REVIEW_REQUIRED`.
- A `ReadyForReviewEvent` at **2026-11-14 07:12:13**, attributed to `radical`.
- Reviewer requests for `adamint` and `ellahathaway` at 07:12:13-07:12:14.

Therefore, "never marked ready" is not the current GitHub state. However, the event
actor alone does not establish whether the person or an automation using their
identity performed the transition. Do not credit the fixer without its operation
evidence. Repeatedly observing a non-draft PR is not performing the handoff.

**Rule:** distinguish validation complete, marked ready on GitHub, reviewers
requested, human approval, and merge. The fixer must perform and verify the
authorized transitions instead of merely writing "ready."

## 2. The ARM64 worker asked a routine question and remained waiting

The owner observed a fixer message beginning:

> Failed asking user: The main and release/17.0 Windows ARM64 runs show the same
> PowerShell archive-extraction failu...

At approximately 05:29 on November 15, the task-status tool returned
`waiting_for_user` for task `299010e0-90da-44e1-80af-29825b794e29`, session
`f70286f5-9a62-485a-b76c-eba95681380e`. The task was created on November 14 at
01:37. Reading the final event page then failed because the MCP server required
reauthentication. The complete question and its exact causal sequence remain
unverified; do not reconstruct them from the truncated message.

**Owner decision:** target the branch from the accepted source failure. A
release/17.0 failure produces a PR against release/17.0; a main failure produces
a PR against main. Seeing the same symptom elsewhere does not require asking
which branch to use. Cross-branch propagation is separate follow-up work.

Updating future prompts does not answer this already-waiting task. An authorized
original-task response or a safely reconciled continuation is still needed.

## 3. Why #22287 stopped progressing

The last inspected work records for
[PR #22287](https://github.com/microsoft/aspire/pull/22287) showed this chain:

1. The original fixer session created the draft PR.
2. The reviewer requested a change to run disposal through the renderer
   dispatcher in the affected tests.
3. A later fixer recorded that it had read and accepted that feedback.
4. The group could not verify completion of the original fixer's outstanding
   command/reconciliation request. The original reply was reported inaccessible.
5. Later fixers refused to modify the same PR branch. The supervisor recorded a
   request for someone with access to the original session, but no such person
   or session was shown to have accepted that request.

This is what earlier reports obscured as "no accepted platform recovery action."
There was no demonstrated running repair behind that phrase. The manager did not
establish an executable handoff, and the supervisor's records did not themselves
wake or obtain a response from someone who could resolve it.

Technical correlation, from the historical records:

- Original fixer session: `ec185e21-33fc-45d0-acf1-1a1934ee9f97`.
- Original run:
  `889fd7b6ec689312aa27d4976a28367a8cef23bc317c8a3d009c689b089d49a6`.
- Outstanding command: `5ba5a6d8-fd8c-5e78-90bc-cb6df973d3c3`.
- Last inspected PR head: `c62d01a7d6278433683babcd2de05aab752d68dc`.

The safety concern applies to overlapping changes to the original branch or
workspace. It does **not** justify blocking independent code inspection,
reproduction, or private validation in an isolated workspace. Current fixer
instructions already permit that distinction; execution and reporting must
honor it.

Fresh original-session settlement was not established by this documentation
pass. Do not treat an expired claim, completed turn, or this explanation as
permission to take over the original work.

## 4. Windows was connected, but the fixer was not using it

At 05:28 on November 15, environment discovery reported the offered Windows
machine **online**, with **zero reported sessions**. The manager's current
workspace was Linux. The fixer definition had no saved default environment.

Earlier fixer records reported that their frozen session-creation tool lacked
environment selection and/or the necessary coordination grant. In contrast,
the manager's freshly discovered session-creation schema exposed an explicit
environment selector. These are different sessions and capability surfaces.

Starting a remote Copilot CLI makes a machine available; it does not move
existing cloud workers onto it or automatically route future work there.
No evidence here establishes that the owner needs to reinstall, restart, or
reconfigure the Windows CLI. A named diagnostic must actually be routed there
and its OS, architecture, tools, and result verified before claiming success.

**Correction:** "Windows unavailable" was too broad. There are separate questions:

| Question | Observed answer |
| --- | --- |
| Is the offered machine online? | Yes, at the stated read |
| Is this manager running there? | No |
| Was a group session reported there? | No |
| Can an existing fixer create work there? | Its earlier records reported a capability gap |
| Can the manager request a new session there? | Its schema supports selection; execution was not tested |
| Can Windows .NET tests run elsewhere? | Yes, the permitted GitHub CI route executed |
| Does any of this prove ARM64 or extension-test readiness? | No |

## 5. One genuine diagnostic execution, not a reproduced failure

The fixer found the existing Windows .NET CI route and dispatched
[run 43675884192](https://github.com/microsoft/aspire/actions/runs/43675884192)
on an isolated investigation branch.

| Evidence | Result |
| --- | --- |
| Workflow interval | November 15, 04:14:28-04:17:17 |
| Head | `63a5c80d6ce88fb6a5ddcd55ef371cbefaef0004` |
| Configuration | One Windows runner, one iteration |
| Selected method | `DcpStartupPublishesDevTunnelUrls` |
| Actual test output | 4 total, 0 failed, 0 skipped |

This proved the diagnostic path worked. It did not reproduce the original
failure, validate a code fix, or establish sustained CI improvement. Consumption
of the result was assigned to the 04:37 fixer pass; that pass was outside the
completed activity audit.

The [.NET reproduction workflow](../../../.github/workflows/reproduce-flaky-tests.yml)
is not a general-purpose runner for native scripts or VS Code extension tests.
Those cases need their applicable procedures and a verified execution path.

## 6. Coordination overhead and avoidable loops

| Finding | Evidence and limitation | Change to pursue |
| --- | --- | --- |
| Public updates on every monitor assessment | Prompt requires a body write, intent, rechecks, and receipt even for freshness-only updates | Publish material changes and a bounded freshness heartbeat |
| Repeated unaccepted recovery requests | Historical #22287, chart #22035, and old monitor records retained requests without an accepted executor | One stable request, a concrete action, and actual delivery/acceptance evidence |
| Broad blocker language | Windows access and original-operation uncertainty were described as incident-wide blockers | State which action is blocked and which actions remain possible |
| Baseline work can masquerade as recovery | Four slices covered one historical day: 56 eligible completed PR-head cohorts, 28 first-attempt workflow failures, causes unknown | Keep historical backfill separate from current repair outcomes |
| Queue time can masquerade as slow execution | Monitor reused one session; its 04:00 occurrence completed at 04:35:12, and the queued 04:30 occurrence was delivered at 04:35:25 | Display waiting and execution timestamps separately |
| Report construction was too expensive | Manager first decoded a 55.7 KB state dump, then extracted fields and read additional histories | Read bounded summaries first; investigate one unresolved question at a time |

No measured billing or CPU savings are claimed. The independent audit took about
17 minutes despite a requested 15-minute bound and could not read hosted state.
Its findings about prompts are useful, but it does not constitute a complete
independent end-to-end review.

## Evidence references

Historical shared records use owner-scoped tag `ci-health`. They are references,
not repository files or guaranteed current revisions:

- `worker/passes/a94fdf75308bef25dfa409912a5693a6ed96ec2f3bbb3daa9a71f07b6c232ed2.json`
  (03:07 source diagnosis).
- `worker/passes/6d2ee26813da32de2eeab2c8a753628c4fc49390e2d50ffa559f75e055453975.json`
  (03:37 CI-path discovery and verdict acknowledgement).
- `worker/passes/e2fdea3b2151221aafce4777d79c660d6109c2a18004090262fe18622b4bdcea.json`
  (04:07 diagnostic dispatch).
- `verifier/current.json` revision 75; `supervisor/current.json` revision 34.
- `assessment/2026-11-15/ci-rates-pr-0400-178bcf35.json`.

The approved prompt generations at the time were monitor 22, fixer 20,
reviewer 18, and supervisor 16. Their configuration adoption does not prove
every new rule was followed.
