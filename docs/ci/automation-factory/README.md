# Aspire CI automation group

Candidate prompts for the existing CIF automations in `microsoft/aspire`.
Published on branch `cif/aspire-ci-group` in the user's `radical/aspire` fork
after `radical/self-workflow` proved inaccessible. This is a prompt-source bundle,
not an Aspire product/runtime change and not a deployment. The automation target
remains `microsoft/aspire`; the fork's repository identity must not be substituted
in automation configuration.

## Operation

| Lane | Contribution | Wake-up | Capacity recommendation |
|---|---|---|---|
| Scout | Classify failures, deduplicate issues, rank work | Every 15 minutes | 1 run |
| Worker | Investigate, fix, open drafts, address feedback and backports | Every 20 minutes plus scoped P0/PR events | 3 runs |
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
`configuration.json` records observed disabled generation-2
IDs, immutable revisions, triggers and tool selections; its concurrency values
are recommendations, not saved settings. It is not an executable API payload.
Runtime model choices are deliberately unspecified.

```bash
python3 docs/ci/automation-factory/aspire/build.py
python3 docs/ci/automation-factory/aspire/build.py --check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s docs/ci/automation-factory/aspire -p 'test_*.py'
git diff --check
```

The generator checks the 12,000-character prompt limit, 32,768-byte inline limit
and exact generated output consistency. It never deploys, enables or runs an
automation. Production still has the older generation-2 prompts.

Local validation before publishing: both generator commands passed; the unittest command passed
5 tests (current output, stale-output repair, exact character boundary, overflow
without partial writes, multibyte byte overflow); `git diff --check` passed.
These validate authoring mechanics, not automation behavior.

## Validation and activation boundary

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

Before activation: finish candidate review and publish source; read current
automation generations, apply approved prompt-only updates by compare-and-swap
and verify authoritative readback. Preserve unrelated settings. Separately set
owner concurrency, confirm artifact claims/responsibility routing and dispatch
behavior, and authorize a bounded test run. Validate controlled conflict, timeout
and crash recovery before broad operation. No automatic enablement or live CI
mutation is authorized by this bundle.

CI Insights, Azure DevOps and retired ci-health claim/run access are not proven.
Retired work stays deferred if old writers cannot be ruled out; do not broaden
credentials or touch `fbd`. The old report issue is explicitly excluded.
The independent deployment execution gate and each definition's enabled state
both control execution.
