# Aspire CI automation group

Prompt sources for the existing CIF automations in `microsoft/aspire`.
Published on branch `cif/aspire-ci-group` in the user's `radical/aspire` fork
after `radical/self-workflow` proved inaccessible. This is a prompt-source bundle,
not an Aspire product/runtime change or an executable deployment tool. The automation target
remains `microsoft/aspire`; the fork's repository identity must not be substituted
in automation configuration.

## Issue-first pilot

The [deployment record](aspire/issue-first-deployment.json) distinguishes saved
prompts from the pending Worker proposal. Scout and Supervisor remain paused;
the enabled Worker still uses its previous prompt until the owner applies the
proposal. Publishing these files does not enable or run any automation.

A maintainer adds `cif` to route the original issue. Scout verifies intake and
adds `cif:ready` after triage; recurring CI trackers remain signals unless
explicitly admitted. Scout recommends dedicated issues rather than creating
them during the pilot. Removing `cif` or closing the issue pauses new writes.
The 25-minute lease includes a five-minute cleanup reserve; expiry does not
authorize takeover of unresolved work. Supervisor reports missed pickups and
recommends closure only after verifying the fix. The queue publisher is unchanged.

| Lane | Contribution | Saved triggers | Saved concurrency |
|---|---|---|---|
| Scout | Screen failures, triage maintainer-labeled issues | Every 15 minutes plus issue-open and label-added filters | 1 run |
| Worker | Investigate, fix, open drafts, address feedback and backports | `cif:ready` added and scoped PR events | 1 run |
| Supervisor | Check missed work, liveness and later results; report human asks | Hourly | 1 run |

GitHub issues are the queue, one edited issue comment is each item's checkpoint,
and expiring artifact claims coordinate cooperating writers. All lanes acquire
item claims before modifying existing work. Claims do not enforce a hard run cap
or make GitHub effects atomic. Waits checkpoint and end; later wakes resume.
Expired claims and missing end logs do not establish that an old writer stopped.
Ambiguous predecessors and unknown mutation outcomes require human recovery.

P0 includes deterministic rolling outerloop failures and widespread unrelated PR
breaks. Intermittent network failures are recorded, not fixed. Rolling workflow
reruns, cancellations and dispatches are forbidden; repository auto-rerun owns
them. Human review and merge remain required. Release work checks main first,
tracks each target and uses backports. Quarantine is a mitigation phase, followed
by a distinct P3 real-fix phase that removes quarantine.

The supervisor reports human asks first, then goals, work and a UTC timeline.
Only it sends consolidated status-issue notifications. It independently checks
source failures against intake, and verifies fixes over seven days with at least
three clean runs per target that actually execute the affected scenario.
The first complete seven-day measurement establishes a baseline; missing
coverage is unknown, not success.

## Sources and generation

`docs/ci/automation-factory/aspire/common.md` plus each lane fragment produces a
standalone file under `docs/ci/automation-factory/aspire/prompts/`.
The generator does not append the historical `self-audit.md` suffix. The
`configuration.json` baseline and earlier observations below are historical,
not the current pilot configuration or an executable API payload.

```bash
python3 docs/ci/automation-factory/aspire/build.py
python3 docs/ci/automation-factory/aspire/build.py --check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s docs/ci/automation-factory/aspire -p 'test_*.py'
git diff --check
```

The generator checks the 12,000-character prompt limit, 32,768-byte inline limit
and exact generated output consistency. It never deploys, enables or runs an
automation. The reviewed prompts were applied with generation compare-and-swap at
generation 3, preserving tools, triggers and responsibility targets. The owner
applied the three enablement proposals, producing enabled generation-4 revisions
on 2026-11-16 at 05:56 UTC. The original disabled baseline is retained separately.

These checks validate authoring mechanics, not automation behavior.

## Historical validation and activation

Parallel Claude Opus 5.5 medium and GPT-6 Astra reviews examined generation 2 and
the first corrected candidate.
Findings prompted corrections for repro timeout consumption, quarantine follow-up,
missed merged/closed events, source closing references, feedback cursors, targeted
outerloop/quarantine reproduction, backports, active-work caps and bounded state.
Opus's claimed missing routing tool was not confirmed: catalog v23 says routing
tools are supplied separately for nonempty `responsibilityTargets`. Actual runtime
availability still needs a controlled run. Review findings are not runtime proof.
Candidate follow-up also identified missing active-state admission, reuse of old
merged fixes after recurrence, ambiguous clean handoffs, P0 capacity reservation,
human-answer resumption and backport provenance from someone else's merged fix.
These were corrected with explicit state transitions, repair-number identities,
durable yielded handoffs and a narrow recorded-backport exception. Non-P0 work
uses at most two of three active-investigation slots, reserving one for P0.
Both reviewers confirmed their blocking findings were addressed in focused
follow-up reads. Crash recovery remains deliberately conservative: without
independently confirmed terminal execution and reconciled effects, a crashed
holder goes to human recovery rather than automatic takeover. This is safe but
may require manual intervention; production recovery behavior is untested.

The following are design scenarios to exercise, not executed production results:

| Scenario | Required behavior |
|---|---|
| Two workers select one item | One verified claim; loser skips; hard capacity set by owner |
| Writer expires or GitHub outcome is unknown | Reconcile; ambiguous live writer/effect blocks takeover |
| Clean wait handoff | Durable yielded record with reconciled effects allows resume |
| Three lower-priority waits | Prevented by reserving the third investigation slot for P0 |
| Verified failure recurs | New repair number/pending targets; old merged PR cannot satisfy it |
| Human answers a blocker | Examine feedback; restore checkpoint state only when resolved |
| Repro exceeds six hours | Consume timeout once, request human help, no duplicate launch |
| Quarantine merges | Finish mitigation targets, return P3/ready for distinct real fix |
| Merge event is missed | Cron reconciles saved target PRs including closed/merged |
| Feedback arrives during a pass | Advance only examined feedback watermark; resume remaining IDs |
| Backport bot is slow | Save request, end, discover exact PR later; no polling/reposting |
| Current state reaches its size budget | Stop affected admissions; preserve lossless history |
| Source/provider coverage is incomplete | Pending denominator and explicit coverage gap |

The owner authorized applying, reviewing, enabling and observing these automations,
and correcting issues found within this scope. Prompt application and enablement
were confirmed by authoritative readback. No manual run was launched. Initial
cron ticks after enablement are scout 06:00 UTC, worker 06:05 UTC and supervisor
06:40 UTC. These due times are not proof of dispatch or completion.

Run-event subscription was rejected with `automation_subscriptions_disabled`;
this manager cannot rely on automatic run notifications. Use persisted run
inspection and captured execution evidence for follow-up checks. Runtime
claims, routing, concurrency, timeout and crash recovery remain unvalidated.
The owner set worker `maxConcurrentRuns` to 5; this differs from the original
recommendation of 3 and must be preserved. It does not change the prompt's
three-active-investigation admission rule.

## Initial execution observations

The 06:00 UTC scout run started and wrote its start record and discovery cursor.
It used `claims/scout` rather than the authored `scout/lease` singleton name.
The correction specifies the exact name and checks the legacy alias
and prior run records before admitting more intake. The owner applied proposal
`0ee41569-fc15-51bc-9378-8c3aa565cc8d` at generation 5.
This correction does not change an already admitted execution or release its lease.
Messaging that execution from this manager was rejected as outside its coordination
grant. No lease was taken over or removed.

The 06:05 UTC worker run finished its initial pass, reporting no eligible queued
issues or CIF PRs and no GitHub mutations; its durable end record exists.
This is not evidence that fix, handoff or recovery paths work. The supervisor's
first cron tick is 06:40 UTC. GitHub notifications for CIF-labelled work were
successfully subscribed as a limited observation path; they do not detect runs
that fail before publishing GitHub effects.

CI Insights, Azure DevOps and retired ci-health claim/run access are not proven.
Retired work stays deferred if old writers cannot be ruled out; do not broaden
credentials or touch `fbd`. The old report issue is explicitly excluded.
The independent deployment execution gate and each definition's enabled state
both control execution.

## Reusable process monitoring

### Artifact-list pagination correction

Worker run `9fc167729f6482492cf64d55260aa20041010e864eb80631f112805ee8efd997`
(11:25 UTC, revision `533c3262-6b4f-4dd1-b1f5-f377258ee851`) made one
`artifact_list` call. Its complete captured response contained a nonempty
`cursor`, but its end-log write claimed "Artifacts (tag cif, no cursor): no
claims/ or handoff/". The captured turn lists no continuation call.
This proves an incomplete scan and inaccurate completeness claim, not an actual
missed claim, handoff or queue item.

The shared correction requires following `artifact_list`'s returned `cursor`
until absent before claiming absence. A bounded partial scan saves the cursor
and reports incomplete evidence; known records should use exact-name reads.
This differs from artifact content paging (`nextOffset`).

| Lane | Applied proposal | Read-back generation / revision |
|---|---|---|
| Scout | `eabae92f-d5e4-5156-8835-a84383f83fdd` | 12 / `db91fe2d-d45b-45a6-baf2-92ed85e9ede6` |
| Worker | `d5732dec-fac1-5bed-872a-51abe8c79a40` | 8 / `2f89b8c1-ec00-466c-a287-e5919aae2dc2` |
| Supervisor | `fdc52a3d-d2ab-51f4-a790-4e460e0e6178` | 10 / `8e80561b-a77e-4c03-869e-cf0f7a29dc4b` |

All three proposals changed only `prompt`; exact saved readback hashes match the
published generated files. The definitions remain enabled with saved
concurrency 1 / 5 / 1 and existing tool/triggers selections intact. Prompts are
11,984 / 11,491 / 11,994 characters respectively. Application affects future
executions; already admitted sessions retain their pinned revision.

Every prompt ends with a self-audit: at most three reusable guidance, process or
tooling gaps, with impact, source and suggestion. Item-specific debugging and
routine discovery do not belong there. Audits are stored in end records and
reported as a final response section; an empty audit is valid.

The supervisor collects these audits and samples captured execution evidence
across all three lanes: at most two runs and ten selected tool records per turn.
It rotates with saved continuation and reports incomplete evidence explicitly.
This is part of monitoring, not another automation. Findings recommend changes;
they never authorize configuration edits, permission changes or merges.
`automation_run_inspect` is in the default tool selection, retained by `tools:null`.
The owner's added tools and direct-update policies are preserved.

Bounded initial samples found:

| Evidence | Finding and response |
|---|---|
| Scout run `95fb8f26f1860bf890986a0969fd818d3b2cdb811638f60f1be4d33cdb99b9e9`, tool `call_Djfr4vR51vNwNu7r3YZoybby` | GraphQL `search` was nested under `Repository` and rejected. Scout guidance now specifies root-level search or paginated direct REST listing, excluding PR entries. |
| Worker run `6ddb796e56977fb879cb48aee01351955da099510b29026cdee80440290fd27b`, tool `call_3d91U3ZXQ5f59eMfLqSSg5Gz` | A different GitHub automation service returned `automations_enabled:false`. Prompts identify this group's owner-scoped tools and prohibit redundant activation-gate discovery. |
| Worker run `a8cb136528ecda7f93b57f2da57540f854eceb24a65ff8418056d1851b4cf347`, tool `call_CD24D7maOxR9bNAyY108qVoI` | `github-read` issuance still returned `credential_not_bound`. Tool selection alone does not bind a credential. Authorized existing reads remain the explicit fallback; no repeated failed issuance within one run. Owner binding remains unresolved. |

These are selected records, not an exhaustive historical audit. The later
worker sample no longer included the unrelated settings lookup, but that does
not validate the updated prompts. No recurring script with enough demonstrated
reuse was established, so no speculative helper was extracted.
Run-event subscription was retried after the tool changes and still returned
`automation_subscriptions_disabled`; this is a server feature gate, not a missing
automation tool. Scheduled supervisor monitoring remains the fallback.

The owner applied the three self-audit proposals. Authoritative comparisons
showed only `prompt` changed and the complete after-values matched the published
files exactly. Enabled readback confirmed scout generation 9, worker generation 7
and supervisor generation 7. The owner's later concurrency edits produced scout
generation 10 and supervisor generation 8: saved limits are **1 / 5 / 1** for
scout / worker / supervisor. These are execution-admission limits, not session
closure or artifact-claim cleanup.

## Follow-up: receipt correctness and session lifecycle

The supervisor created the status issue at
https://github.com/microsoft/aspire/issues/22302 and detected four incorrect
first-attempt success dispositions in the initial scout's PR coverage artifact.
Direct `/actions/runs/<id>/attempts/1` reads independently confirmed three
failures and one `action_required`; the old 55% claim is not reliable.
For run `43725765894`, captured tool `call_IleB3J9TWqthDjpjl0Zb3kZX`
queried `/attempts/1/jobs` without pagination and returned `[]` after filtering
for failures. A paginated read found 451 jobs and four failed jobs; the first
failed job was outside the default 30-entry page. This establishes a concrete
failure mode, without claiming it explains every incorrect row.

The scout follow-up requires the attempt-specific run receipt for conclusions,
complete job pagination for causes, code-derived counts and append-only
corrections after checking supervisor assessments. An empty failed-job page
must never become a successful-run conclusion.

| UTC | Observed scout evidence | Interpretation |
|---|---|---|
| 06:30 | Run `e338383dd84372a58705e9ba65a8615255bb3029eb80fd14c86cd8cff2eb29a1`, session `05983453-fc32-45e0-b166-ffb749571578`; captured turn ended at about 06:45:52 | Its `scout/lease` remained held until 07:12:26; no clean completion handoff established |
| 06:45 | Another scout run admitted before the earlier turn ended | Brief overlapping execution records; not proof of simultaneous GitHub writers |
| 07:00 | Run `bca48c13477e1cc1e321eed49cca2048e616a2ef936b40c432326ee73f1c8fb7` deferred; captured response reports no GitHub work and a reusable self-audit | The lease guard prevented intake; expiry alone cannot resolve the predecessor |

Do not confuse finishing a turn, releasing a lane lease, releasing an item
routing responsibility and closing a session. The supervisor follow-up
distinguishes unfinished handoff recovery from genuinely finished idle-session
cleanup, retaining owners needed for PR feedback. Where selected and eligible,
`reconnect_session` may request one reconciliation-only resume of the original
idle/settled worker, retaining its operation UUID and verifying the resulting
handoff artifact. It must not replay business effects or dispatch a replacement.
Denied or ambiguous recovery goes to the owner.

Catalog discovery found `get_session` (coordination-scoped persisted lifecycle
status) and `reconnect_session`, but no session-close capability. The manager
cannot directly inspect or close arbitrary automation sessions with its frozen
tools. Cleanup candidates must therefore be reported, not called closed.
Neither idle UI state nor persisted run completion establishes a live resource
leak, successful release, or permission to kill a process.

The follow-ups were published in `1ac56ca0f`. Both authoritative proposal
comparisons were complete, changed only `prompt`, and exactly matched the
published bytes. The owner applied supervisor proposal
`1999f002-f581-5432-98af-4bf9c2d66e32`; enabled generation 9 readback
confirmed the lifecycle instructions with concurrency 1 unchanged. Scout
proposal `d75d15de-d04d-535c-bec8-9a5d5fdba287` remains pending owner
application; saved generation 10 has not yet adopted the receipt correction.
These changes affect newly admitted runs, not existing executions.

At 07:22 UTC the capacity endpoint reported no active or queued executions for
the three lanes, so increasing concurrency would not address the observed
stall. The 07:15 scout's captured turn again reported deferring for the 06:30
predecessor. A fresh lease read returned absent after its recorded expiry,
which does not reconcile the earlier run. Its final captured tools are reads,
not a completion handoff; why it stopped remains unknown. No original-session
recovery or session closure is claimed. The next saved supervisor due time is
07:40 UTC, an opportunity for recovery inspection, not proof it will succeed.

## Recovery follow-up at 08:49 UTC

The supervisor admitted one reconciliation-only resume of original scout
session `05983453-fc32-45e0-b166-ffb749571578` using operation
`07ffb82e-0903-4a9d-846e-aa7df63f23a8`. The controller reported delivery
at 07:46, then `resumeStatus=reply_available` at 08:40:45. The latter result
also returned `recovery_reply_restricted`: cross-session reply text was not
exposed. The supervisor reports no new original end/handoff artifact.
This is not verified recovery, permission to retry the resume, or proof of a
session-release fix. Do not seek the restricted reply through another route.
The owner recovery ask is tracked in
https://github.com/microsoft/aspire/issues/22302#issuecomment-6911747022.

The owner applied the scout receipt proposal at 08:48:07. Enabled generation 11,
revision `85bfed82-204c-4058-8229-5affe494fb88`, retains concurrency 1 and
the existing tools/triggers. The proposal's complete after-value matches the
published 11,907-byte prompt; application is not evidence that the historical
receipts have already been corrected. Previously admitted executions do not
gain the new instructions.
