# CI Shepherd hybrid daily pilot

`pilot` mode manages labeled open issues and PRs in `radical/aspire` only.
The daily 09:17 UTC sweep is explicitly opt-in. Disabled, unconfigured and
unchanged waiting sweeps skip the actual native job, not just its prompt.
The existing `transport-proof`, `observe` and `live` fixture modes retain their
legacy contracts; the pilot never adopts PR121 or writes its canonical comment.

## Pilot setup and stop controls

The operator must create one open tracker **issue** and one canonical authority
comment authored by `radical` (numeric user ID1472). Print the initial body with:

```shell
PYTHONPATH=.github/workflows/ci-shepherd python3 -B -c \
  'import pilot_state; print(pilot_state.render(pilot_state.new_ledger()))'
```

Post that exact body once, then configure these repository variables:

| Variable | Value |
| --- | --- |
| `CI_SHEPHERD_ENABLE` | `true` to enable; absent/other values disable |
| `CI_SHEPHERD_TRACKER` | Tracker issue number, never121 |
| `CI_SHEPHERD_TRACKER_NODE` | Independently verified tracker REST `node_id` |
| `CI_SHEPHERD_AUTHORITY_COMMENT` | Exact initial authority comment ID |
| `CI_SHEPHERD_REMINDER_DELAY_SECONDS` | Optional whole seconds, 1-86,400; default `60` |

The existing `CI_SHEPHERD_USER_TOKEN` secret is used only by trusted collection,
settlement and publication jobs. No new authentication mechanism is introduced.
At runtime, `/user`, `/users/radical`, repository ID746880239, `radical/aspire`,
default branch `main`, hosted OIDC identity, tracker node and exact comment
ownership are reverified. Missing, malformed, replaced or duplicate authority
fails closed; the pilot never silently creates a replacement ledger.

Label an issue or PR `shepherd-adopted`. `shepherd-hands-off`, adoption removal
or closure stops new item writes. Re-adoption retains every original counter
and reservation. Disabling `CI_SHEPHERD_ENABLE` stops new pilot mutations and
inference. None of these controls promises cancellation of an already-running
cloud worker. Humans merge and close work.

A native human handoff remains paused while adoption stays unchanged. The fixed
upstream trial supports an explicit local `resume` admission change described
below; a policy update alone never reopens a handoff. The existing observed
stop-to-adopt transition also retains the same chain, never a new allowance.

The repository authority tracks chain mappings, operation identities, billing,
reservations and disposition IDs, not log/feedback bodies. Its body is limited
to60,000 UTF-8 bytes; exhaustion requires human attention, never history
truncation or a budget reset. Per-chain presentation comments are updated in
place and are not another authority. Exact owned presentation IDs/markers are
excluded from feedback; other operator comments remain legitimate input.

GitHub API JSON responses have a separate 8 MiB bound in `live.py`, enforced by
both the HTTP reader and strict JSON decoder. A 100-check page includes full
metadata and can exceed the 256 KiB bound used by internal action packets;
accepting those responses does not expand agent inputs, tracking records or
credit allowances. Complete pagination and duplicate-key/non-finite JSON
rejection remain required. Oversized responses report the API bound explicitly;
uncertain write results remain non-retryable.

GitHub's `Link` header can use `/repositories/<id>/...` instead of the requested
`/repos/<owner>/<name>/...` path. The pilot accepts that alias only for its verified
repository ID, with the same endpoint, head and page query. Subsequent requests
are rebuilt from the pinned named path, never followed from response URLs.

## Delayed human reminders

An enabled cheap sweep records explicit PR blockers: a current-head workflow
run with `action_required`, a saved worker waiting for user input, or a native
human handoff. Ordinary pending CI, drafts and review waits do not start timers.
Workflow run IDs, repository/head identity and complete pagination are verified;
unreadable evidence is unknown, never proof of approval or green CI.
Unknown current-head workflow evidence pauses both native admission and repair
effects until a complete fresh read succeeds.

After the delay, the next cheap sweep rechecks the head, adoption, takeover,
blocker and authority before posting one fixed `[automated] @radical` comment
with a workflow, task or PR link. This is earliest eligibility, not exact
delivery: the existing daily/manual schedule is unchanged.

The optional per-chain reminder in `pilot_state.py` preserves old authorities
without migrations or counter resets. Resolution, a new head or a different
blocker kind starts a new episode; changing workflow run IDs does not.
Unknown workflow or saved-task reads retain the timer and send receipt without
sending. The send boundary is persisted first; an uncertain POST never blindly
retries. An exact owned marked comment can confirm a lost
response. Owned reminders are excluded from repair feedback. Notifications use
no native rounds, worker slots or credits and do not approve workflows or merge.

Both profiles support reminders. The upstream PR20722 comment capability accepts
only this fixed body and a host-generated episode marker, not general comments
or edits. Disabling the pilot or removing adoption stops new notifications.

## Lifetime policy and accounting

- Up to two local attempts and ten total action rounds per issue-to-PR chain.
  Failed inference/validation counts. Fresh runs, new heads and verified child
  PRs never reset counters. Unsupported local scope escalates immediately;
  cloud escalation is sticky.
- Native admission reserves30 credits. Fork chain allowance is500 credits;
  the fixed upstream trial's allowance is1,000. The repository allowance is
  1,000 in a rolling24-hour window. Cloud admission
  reserves the remaining chain allowance. Actual native `ai_credits` and
  terminal task-session `ai_credits` nano units settle separately, including
  failed native runs. Missing/premium-request billing is unknown, not zero:
  its reservation survives the rolling window.
- Verified terminal workers complete their operation and result/artifact
  bookkeeping even when usage is absent or partial. Unknown costs still
  retain conservative credit reservations, blocking new paid work when the
  remaining headroom cannot cover admission; later
  reported usage settles the same operation without another round.
- Fresh direct reads of saved task IDs can resume previously completed work.
  Every saved task is verified again, including completed receipts; unknown sessions
  hold a worker slot and remaining credit reservation. Additional reported
  session usage is cumulative and never overwrites billing with a smaller total.
- At most two tracked in-flight cloud workers per tracking authority, including
  unknown send outcomes. Foreign tasks are neither inspected nor counted.
  One chain cannot begin another
  action while its mutation is reserved, waiting or uncertain. Polls, CI waits
  and human waits do not consume rounds or native inference.
- These are admission/accounting limits, **not hard billing caps**. An admitted
  response or worker can overshoot. Overshoot is recorded and blocks later
  admission. Definitive no-send rejection releases worker capacity; an uncertain
  POST never retries.

  Pending current-head CI pauses native admission and repair dispatch, including
  fresh review feedback. Complete terminal infrastructure/cancellation-only CI
  waits cheaply for recovery or a required rerun, without a sticky human handoff.
  Fresh review feedback can proceed as **review-only** repair while that CI is red.
  Failed/error commit statuses and unknown or genuine failing checks prevent
  infrastructure-only classification even when their feedback was dispositioned.
  Older completed workflow runs superseded by a newer terminal run of the same workflow
  and event on the same head do not keep recovered CI waiting. Actually pending
  runs are never suppressed. A terminal workflow failure without failing jobs
  still supplies explicit unknown-cause investigation feedback.

  The shared observation projects untrusted check output and annotations from
  IDs derived only from the verified named-repository exact-head check connection.
  The annotation endpoint returns a bare, idless array with nullable text fields;
  `live.API.pages(identity_key=None)` is explicit for this collection, while
  ordinary inventories still require identities. Annotation reads have a shared
  ten-request/page and 32,000-byte serialized aggregate bound and must match
  `output.annotations_count`. Missing, malformed, incomplete or unavailable
  diagnostics are logged as unknown, never successful empty evidence.
  Positive infrastructure classification requires complete failure-level runner
  disconnection evidence; a runner-scarcity NOTICE is not enough. Cancellation
  alone establishes a rerun requirement, not an outage.

  Aggregate dependency gates remain in inventory/readiness. When source or failed
  steps establish that a gate merely reports dependent-job failure, the worker
  investigates those underlying failures rather than changing the gate. For
  example, `ci.yml`'s Final Results job reports dependency status, and
  `analyze-ci-failure.lock.yml` uses failed-step evidence to distinguish gates.
  A check name alone never authorizes ignoring it; unknown aggregate evidence
  can conservatively remain investigatable. No job-list API scope is added.

  Before reserving native credits or a round, `pilot.py` bounds the complete
  serialized worker request to 20,000 bytes, including JSON escaping, instruction
  preamble, authority correlation and every feedback ID. Descriptive strings can
  be shortened with an explicit truncation marker; both native and worker receive
  the same projected evidence. Mandatory fields that cannot fit pause visibly
  without inference/reservation. Internal packets remain limited to 256 KiB.
  An unattempted task POST rejected by any fresh dispatch guard releases only its
  unused worker reservation, retaining native billing and the consumed round.
  Attempted/unknown writes and uncertain authority publications remain
  non-retryable and retain capacity protections.

The single serialized workflow sweeps the adopted intake and fetches only the
distinct task IDs saved in its authority, then rechecks those tasks before a
new cloud send. Repository, requesting actor, session operation marker and
branch identities must match. An unreadable or malformed completed task becomes
unknown/pending again, retaining known spend and restoring its reservation.
Every session must report a known state. A terminal task with any nonterminal
session is conflicting evidence and remains unknown/pending; historical
terminal session outcomes need not match the aggregate task outcome.
An uncertain POST without an ID needs human verification, never automatic
discovery or another POST. Exact-head/adoption/authority guards remain in place.
The persisted cursor chooses due chains fairly;
a waiting or exhausted chain does not monopolize the next action.

Each PR also has a bounded read-only Copilot work-event history, obtained by
the exact sealed GraphQL POST in `pilot_history.py`. Repository database ID and
PR node are matched to authenticated REST identity. Starts, finishes, failures,
requesting users and nullable session IDs are descriptive context, not task IDs
or proof of current execution. Old unmatched starts do not block work. Unknown
or incomplete history is explicit and never changes repair fingerprints,
worker slots, billing or rounds.
Raw history is excluded from prepared action artifacts and the bounded worker
request. Cheap hosted logs retain counts and the last five events without
displacing source or repair feedback.

Every enabled sweep prints a readable subject/head, tracked task/state,
next action, actual credits/reservations and PR history in its hosted prepare
log, including waiting sweeps with no native packet. This needs no model call
or a status comment writer. Unknown billing retains reservations; task
completion alone is not a real current-head CI or approval result.
Finished workers with unsettled billing are reported separately from active
work or uncertain sends. The local runner returns `observed; no inference`
with a billing reason and an explicit round-limit indicator for this case.
It does not infer a zero cost, release the hold or increase spending limits.

Current-head checks/statuses and explicit review records determine readiness.
Approval requires a current-head `APPROVED` review whose reviewer has not been
re-requested; drafts, conflicts, blocking reviews and pending CI are not
merge-ready. Supported same-PR feedback is batched into one decision/task.
Addressed, declined and needs-human dispositions survive subsequent sweeps.
An all-declined PR native batch using `human` or `cloud` completes without a
worker or sticky handoff/reminder. Its declined dispositions, actual native
billing and consumed round persist; the chain stays open for fresh feedback.
A sticky PR native handoff requires at least one `needs-human` disposition.
Issue-body implementation/handoff and actual inline patches retain their
semantics independently of comment dispositions.
Completed cloud tasks conservatively disposition their old feedback as
needs-human rather than pretending task completion proves resolution.

Issue-to-child adoption requires exact operation correlation in every task
session, authenticated task creator/repository, actual GitHub PR database ID,
branch artifact and independently fetched REST PR and Git ref mapping. Only
then may the trusted host add the fixed `shepherd-adopted` label to that child.
The original chain owns all its counters and spend. Missing PR artifacts cause
an honest human handoff, not another initial assignment or guessed PR.

## Supported inline profile and credential isolation

The intentionally narrow `python-labels-v1` profile changes only
`.ci-shepherd-pilot/labels.py`; exact-head
`.ci-shepherd-pilot/test_labels.py` remains unchanged. Eligible PR diffs modify
only that existing source file (`status=modified`); added fixtures or test changes
use the cloud lane. PR file inventories are identified by unique `filename`
because the primary API does not supply item IDs. The source has one single-parameter `normalize_label`
function with optional inert `str` annotations. Repairs preserve its signature,
returning its parameter or a chain of zero-argument `strip`, `lower`, `casefold` and
`upper` calls. Source/replacement bodies are limited to16,000 bytes and
64 changed diff lines. No imports, globals, test-runner monkey-patching,
commands, arbitrary paths or executable API bodies can be proposed.
Other PRs and all initial issues use the generic cloud lane.

### Manual upstream trial

The controller remains installed/executed in `radical/aspire`; its checkout,
OIDC identity and existing daily fork schedule do not change. Manual dispatch
with `mode=pilot`, `target=upstream-20722` selects only `microsoft/aspire`
(repository ID `696529789`), PR `20722`, existing branch
`copilot/restrict-workflows-to-microsoft-aspire`, base `main`. Scheduled runs
always select `target=fork`. There is no upstream workflow installation,
schedule, general repository selector, host Git Data writer or general
comment writer. Only fixed delayed human reminder comments are permitted.

Use a **separate fork-hosted authority**, not tracker122 or its comment:
`CI_SHEPHERD_UPSTREAM_TRACKER=127`,
`CI_SHEPHERD_UPSTREAM_TRACKER_NODE=I_kwDOLIR8788AAAABU-aGuQ`, and
`CI_SHEPHERD_UPSTREAM_AUTHORITY_COMMENT=<verified new radical-owned comment ID>`.
Render the empty target namespace locally before publishing it:

```shell
PYTHONPATH=.github/workflows/ci-shepherd python3 -B -c \
  'import pilot_state; print(pilot_state.render(pilot_state.new_ledger("microsoft/aspire")))'
```

The ledger's target namespace must match the fixed binding while its tracker
and writer remain in the controller fork. Existing fork/legacy authority is
never repointed. Native admission is still30 credits, upstream chain allowance1,000 and
rolling authority allowance1,000; these are independent authority namespaces,
not a promise of a combined cross-authority billing cap.

Both profiles count only Shepherd-started in-flight/unknown operations saved
in their own authority toward max2, not a combined fork/upstream limit.
Ownership requires the saved task ID and verified operation/session correlation,
not just a creator login. Neither profile lists the global task catalog or
inspects foreign tasks. Other agents can still edit the PR: there is no claim
of exclusive ownership, and fresh head/adoption/takeover guards remain required.
Failed own-task reads retain pending state, capacity and unknown credits.

The trial has **ten total lifetime native action rounds**: at most ten worker
requests, then observation/billing only. The fork profile also has ten rounds.
The binding's `round_limit` in `pilot_binding.py` governs admission, fresh repair
effects and status; it is policy, not a field in the persisted JSON. Existing
rounds, operations and credits survive failure, restart, head change and
stop/re-adoption without resets. Unknown sends remain reserved and block a
new round until reconciled.
Raising the round limit does not change credit allowances or refund unknown
worker billing. A completed but unbilled worker can still block a new paid round.
The worker must refresh the full fork authority URL before writes, and may
investigate and repair one bounded batch of ordinary current-PR CI/review
feedback. Failed job names alone do not diagnose a cause or justify a handoff:
unknown failures require investigation of logs, artifacts and annotations.
Only a verified cause warrants a repair, with at most one actual minimal
non-forced commit; no artificial commit is required. Human handoff is for a
concrete human-only blocker. Intentional generated workflow updates retain the
pinned gh-aw v0.89.17 compiler. Labeler workflows, action-pin bypasses,
authentication changes, permission broadening, test weakening, merge and
force-push remain forbidden. No automatic workflow reruns are authorized.
Inline review path/line metadata is preserved. A fixed diagnostic brief is
included only on its exact observed head; changed heads do not inherit it.

Verified saved-task results enter the fresh packet as bounded `workerResults`:
task/session states, mapped artifacts, optional session errors and source-head
comparison. The [task API](https://docs.github.com/en/rest/agent-tasks/agent-tasks#get-a-task-by-repo)
does not expose a final narrative or logs. Missing narrative and unchanged head
alone do not justify a human handoff. Current PR, checks and review evidence
determine unfinished work; task completion and artifacts never prove repair,
comment resolution or green CI. A fresh decision must diagnose an unsuccessful
attempt rather than blindly repeat it.

Completion does not synthesize `needs-human`. Remaining current feedback can
be evaluated again within the existing budgets and round limits. New operations
record the native `feedbackDecisions`: cloud `addressed` requests repair, not
verified resolution; explicit declines and human items persist. Only legacy
controller completion entries tied to a freshly verified completed worker's
exact batch can re-enter feedback. Later explicit decisions, ambiguous native
completion, unknown receipts and real human handoffs remain excluded. Old
dispositions, operations, counters and billing holds are not deleted or reset.
Skipped local sweeps report the per-chain next-action reason; unknown billing
is a retained-hold note unless it actually blocks admission.

Global disable still forbids native admission, task dispatch and publication.
An authenticated settlement job may record available usage for an already
admitted packet while disabled, then safely finalize an unsent operation.
Potentially sent effects retain their state/capacity; unknown usage retains
its reservation. Missing authority configuration never authorizes settlement.

The same fresh native engine emits only a typed proposal through
`submit_decision`. It receives no GitHub write token or shell/write tools.
The pinned compiler's `engine.env.GH_AW_MAX_AI_CREDITS` override binds the actual
AWF execution config to 30 for pilot/schedule and 5 for legacy modes; an expression
in `max-ai-credits` frontmatter alone is not emitted by v0.89.17. The behavioral
budget test executes the generated config statements for every supported mode.
A separate validation job executes this host-owned argv:

```shell
python3 -B -m unittest discover -s .ci-shepherd-pilot -p 'test_*.py' -v
```

Validation runs in `python:3.13-slim` Docker, with no network, capabilities,
credentials or writable host checkout; CPU/memory/PID limits, a read-only mount
and a90-second timeout bound it. Docker/image availability remains a hosted
acceptance prerequisite. Unit tests exercise this boundary using process fakes,
not a claim that hosted Docker has already run.

Validation evidence binds the exact original/replacement bytes, head,
operation and trusted argv. The separate publisher never checks out or executes
PR code/hooks. It creates one source blob/tree/commit through Git Data APIs,
rechecks the exact branch/head/adoption/authority, then updates the ref with
`force: false`. Native billing settlement runs even when decision evidence is
missing, malformed or unsuccessful; such evidence cannot authorize code.

## Local upstream pilot

`local.py` runs the same `pilot.prepare` / `pilot.settle` controller and policy
for the fixed PR20722 trial on a POSIX machine. GitHub Copilot repair workers
remain remote. The Actions workflow stays available for later hosted execution;
its GitHub-hosted identity checks are unchanged, not emulated locally.

The operator explicitly selects the existing tracker/comment and authenticates
as radical through `gh`. The adapter verifies the same actor/repository/tracker
identities as the hosted pilot. It never initializes or resets the authority.
Tracking remains in the existing GitHub comment, not local files.

Before local effects, disable the hosted Shepherd workflow and finish or cancel
its existing runs. The local adapter rechecks disabled state and complete hosted
run inventory at authority/effect boundaries. A machine-wide per-authority lock
also rejects a second local controller, even with a different output directory.
The global `CI_SHEPHERD_ENABLE` variable and item takeover/removal still apply.

```shell
gh workflow disable ci-shepherd.lock.yml --repo radical/aspire

python3 -B .github/workflows/ci-shepherd/local.py observe \
  --tracker 127 --authority 5983713607 \
  --tracker-node I_kwDOLIR8788AAAABU-aGuQ \
  --workdir artifacts/ci-shepherd/local

python3 -B .github/workflows/ci-shepherd/local.py watch \
  --tracker 127 --authority 5983713607 \
  --tracker-node I_kwDOLIR8788AAAABU-aGuQ \
  --workdir artifacts/ci-shepherd/local --interval 60
```

`observe` is read-only and needs no hosted disable. `run` executes one sweep;
`watch` repeats cheap sweeps and invokes inference only when shared admission
selects due work. Commit reviewed source first: live modes reject dirty source
and stop before another sweep, repair or notification if the source changes. Billing
settlement remains independent of that source check.

`resume` is an explicit **no-inference** mode for an existing completed native
human handoff on the fixed upstream PR. Supply the exact latest operation ID and
an independently fresh-read current head; stale operation/head, takeover, missing
child/worker handoffs, unknown billing/sends and exhausted prospective budgets
are rejected. It uses the same POSIX authority lock, clean reviewed source,
global enable and hosted-disable controls, but requires no Copilot CLI.

```shell
python3 -B .github/workflows/ci-shepherd/local.py resume \
  --tracker TRACKER_NUMBER --authority AUTHORITY_COMMENT_ID \
  --tracker-node TRACKER_NODE_ID \
  --operation EXACT_LATEST_COMPLETED_NATIVE_HANDOFF_ID \
  --expected-head FRESHLY_VERIFIED_CURRENT_SHA \
  --workdir artifacts/ci-shepherd/local
```

Resume directly verifies every saved task ID without sweeping/reconciling
workers. It only opens the chain, removes `needs-human` dispositions from that
latest exact batch and clears its matching native-handoff reminder (including
a confirmed reminder receipt). Declined/addressed and older dispositions,
operations, task/session IDs, rounds, billing and reservations are unchanged.
Overlap with older completed worker feedback is rejected as ambiguous.
An existing completed native all-declined PR handoff can also be explicitly
resumed under the same guards: the exact saved batch must be entirely declined,
no dispositions are unmasked, and only the chain state and matching reminder
change. A batch mixing addressed/declined without `needs-human` does not qualify.
Observation/freshness comparison happens after unmasking, so reopening into
pending or infrastructure-only CI is allowed without dispatching anything.
Same-head reruns can replace old CI feedback IDs or recover to green; those IDs
need not remain live for an explicitly authorized resume. Removed non-CI feedback
must still match current source feedback, and publication binds a fresh current
observation rather than replaying the old check inventory.
A separately executed fresh `run` can spend an eligible remaining round only
when actionable; `resume` never starts a worker, decision or CI rerun.

**Every admitted decision uses a fresh agent.** A new Copilot1.0.92-3 process,
UUID, temporary home and working directory outside the checkout receive only
the actual workflow prompt body populated by the current `pilot.prompt(packet)`.
No resumed conversation, user/repository instructions, memory, plugins or
follow-up coaching are supplied. The sole tool is `safeoutputs-submit_decision`,
implemented locally by a stdio server that can only record one proposal.
CLI approval uses `--allow-tool 'safeoutputs(submit_decision)'`; the exposed
`safeoutputs-submit_decision` name is an availability filter, not a permission
pattern. See `copilot help permissions` for this distinction.
The same host-event validation requires a fresh session, verified version,
exact tool grants, one successful submission and matching final JSON.

Inference uses the operator's Copilot allowance and the existing30-credit native
admission setting. The CLI authentication token is kept out of prompts and
stripped from tool environments; the decision server has no GitHub client.
Local execution is not a hosted firewall/permissions proof.

Actual native billing uses the same cumulative
`session.usage_checkpoint.data.totalNanoAiu` source as pinned gh-aw, divided by
1e9. Missing billing retains the reservation; a failed decision cannot start a
worker. Process, session, prompt, normalized usage and result receipts are kept
under the selected output directory. Native failure or uncertain send stops
the runner rather than silently spending another round.

Stop the local process before re-enabling the hosted workflow. Do not run both
controllers or edit the tracking comment to recover a stalled operation.

Task creation uses only the verified primary REST fields `prompt`, `base_ref`,
optional `head_ref` and `create_pull_request`, API version `2026-03-10`.
Task details come from `agents/repos/{owner}/{repo}/tasks/{task_id}`; both
`is_archived=false` and `is_archived=true` inventory lanes are required only by
legacy fixture modes. Normal pilot profiles read saved task IDs only.
Primary task responses require only `tasks`; lane counts are optional and
validated when present. Without counts, bounded Link pagination establishes
completeness. Live links can omit the archive filter: every next request keeps
the original filter pinned, and contradictory filters/relations fail closed.
A full final page requires an explicit self `last` link; a missing continuation
or exhausted page bound is not an empty/complete inventory. See the
[primary REST schema](https://github.com/github/rest-api-description/blob/main/descriptions/api.github.com/api.github.com.json).
No invented worker hard cap or cancellation endpoint is exposed.

## Pilot validation

```shell
python3 -B -m unittest discover -s .github/workflows/ci-shepherd/tests -p 'test_*.py' -q
artifacts/ci-shepherd/tooling/gh-aw compile ci-shepherd --validate --no-check-update
artifacts/ci-shepherd/tooling/gh-aw lint .github/workflows/ci-shepherd.lock.yml --shellcheck
```

Python contracts cover lifetime2/10 boundaries, shared reservations, unknown
usage, replay/uncertain sends, independent chains, task/child mappings, takeover,
stale heads and isolated exact-byte validation. The dedicated
`validate-agentic-workflows.yml` already consumes every Shepherd Python/policy
input and runs these tests; no new selector-gated consumer is introduced.
Actual hosted inline repair, automatic exact-head CI, cloud progress and usage
remain separate fork-hosted acceptance gates.

## Legacy guarded fixture gate

The manual workflow defaults to `transport-proof`: a fresh native decision and a
receipt with `outcome: wait` and empty `effects`. `observe` performs deterministic
GET-only collection and fresh reasoning without starting a trial. `live` installs
one capability: repairing current-head label-normalization failures in
`radical/aspire#121` on the existing `shepherd-fork-fixture` branch targeting `main`.
Only `.ci-shepherd-fixture/labels.py` may change; fixture assertions must not.

`hosted.py` prepares host-owned artifacts and applies independently downloaded
same-run inputs. `live.py` implements the fixed REST adapter, independent history
witness and existing-PR executor. `policies/pr.md` is the packet-first reasoning
and output policy. No issue lane, unrelated defect repair, rerun, checkpoint
mutation, merge or force-push handler is installed. Local live selection is
rejected before credential loading; the callable core remains dry-run by default.

## Reconciliation safety contract

`round.py` exposes `prepare_reconciliation`, `validate_reconciliation_decision`
and `apply_reconciliation`. This is distinct from the transport-proof schema;
the existing smoke and hosted workflow still use their original contracts.
Apply defaults to dry-run. Effect-bearing execution requires an explicitly
injected host executor, writer capability and validated fresh host session/tool
evidence through `reasoning.py`. There is no executor registry or model-supplied
API body, executable command or permission report.

Both prepare and apply require a separate `receipts.TrialScope(root, trial)`
argument. It is trusted host authorization, **not** a packet/decision field.
The root comes from immutable approved host configuration, not adopted-candidate
selection or an agent claim. Only that one authorized root is accepted, including
when another root has complete inventories or the original trial has expired.

Use `trial=None` only for a genuinely unstarted authorized root. Its immutable
`trialId`, `trialStartedAt` and `expiresAt` are pinned at authorized live
initialization and cross-checked against the authenticated remote record before
every subsequent mutation. Reconstructed cycles supply that same pinned tuple
from trusted authenticated authority; an existing record with an unbound or
mismatched scope fails closed. Missing authority cannot restart a bound trial.
Observe, wait, dry-run and transport proof do not start its clock.

- Decisions bind schema, subject, root, policy, packet/run identity, revision,
  feedback and evidence references to the host prepare packet. Duplicate JSON
  keys, extra fields, invented evidence and unsupported arguments fail closed.
- The vocabulary is `wait`, `repair-pr`, `assign-issue`, `adopt-pr`,
  `rerun-transient` and `checkpoint`. Wait has no arguments. Other arguments are
  typed references to host-observed feedback, linked PRs or verified transient
  current-head jobs, not freeform mutation instructions.
- Before **each** status write and effect, apply refreshes identity, open state,
  adoption, hands-off, head/issue revision, full normalized feedback, linked
  subjects, jobs, inventories, authority, budgets and expiry. A root issue or
  linked PR hands-off veto pauses the entire chain; removed adoption stops
  management. No progress publication follows that veto.
- `issue_pr.py` requires explicit complete inventories for subjects, feedback,
  workers, **both archive lanes**, managed PRs, history, comments and jobs.
  Unknown task states hold capacity. Only `completed`, `cancelled` and `failed`
  are terminal. A previously confirmed worker missing from the inventory remains
  unknown, not free capacity; losing archive visibility does not discard its ID.

`receipts.py` owns one bounded root record in a marked `[automated]` status
comment, edited in place. Both the GitHub actor ID and login must match the
trusted host actor; marker text from another actor is ignored. Malformed,
unsupported, ambiguous or mismatched authoritative records require recovery,
not a new success-shaped chain. Local files are never authority.

The bounds are enforced by `round.py`, `issue_pr.py` and `receipts.py`:

- A prepare packet is valid for ten minutes. Every apply clock read uses a local
  high-water mark, including initialization and limit checks, so a rollback
  below an earlier apply observation fails closed even while the packet remains
  valid. A reconstructed trial cannot authorize observations before its pinned
  start; both the checked clock and receipt limits enforce that lower bound.
- The remote root trial has an immutable ID and absolute 24-hour expiry, started
  only by its first live initialization. Dry-run and implementation approval
  start no clock. Process loss, readiness, new heads and fresh packets never
  renew it.
- One active worker is allowed; unknown workers and nonterminal or uncertain
  reservations retain capacity through takeover and expiry.
- Each chain has three cumulative repair reservations. Transient reruns have
  two reservations per PR, stable logical job and head, independent of run/job
  IDs. Reservations never refund the cumulative budgets, even on known failure.
- Initial issue assignment has one intent per chain, including after terminal
  workers or issue revision changes. Further work must use the managed PR lane,
  not repeated initial assignment to bypass repair limits.
- The normalized repository inventory also limits open managed PR capacity to
  three when allocating a slot through issue assignment or child PR adoption.
  Repairing an existing managed PR allocates no slot, and adopting a PR already
  counted by that complete inventory allocates no additional slot. Worker,
  repair-budget and completeness guards still apply. No merge, force-push,
  approval dismissal or test weakening is exposed.
- The status body is at most 16 KiB. Exhaustion pauses instead of truncating
  history or resetting counters; larger details belong in external artifacts.

Operations retain stable typed basis identities and pass through `prepared`,
`reserved`, `consumed`, then `confirmed`, `failed` or `uncertain`. Preparation,
budget reservation and the send boundary are persisted **before** an effect.
Only a verified returned ID or an exactly correlated remote outcome may confirm
it. Replays never repeat an effect. An established past worker outcome can be
confirmed before considering a new head/action, without dispatching a new worker.

A lost POST response is reconciled only by exact remote root/operation identity.
An unestablished result requires human recovery and zero retry POSTs. Lost
status-create/edit responses are recovered by rereading authenticated authority
and comparing the entire expected record; ambiguous or absent publication
does not authorize another create or reset the trial.

### Collector and history authority

`github.py` remains an injectable primitive. The fixed `live.py` collector
verifies the selected `/user` identity against `/users/radical`, and pins the
repository, PR database/node identity, creation timestamp and branch mapping.
It collects both explicit task archive lanes, task details/sessions/artifacts,
all issue/review feedback, managed open PRs and current-head fixture runs/jobs.
HTTP errors, missing bodies/statuses, oversized data, paging errors, foreign links,
skipped/cyclic pages and pagination-limit exhaustion are explicit failures;
a full page requires another probe. See the primary
[comment API](https://docs.github.com/en/rest/issues/comments) and
[pagination contract](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).

Real failed unittest logs are descriptive, host-bound context alongside the
unchanged closed core packet. Current-head actionable fixture assertions become
open typed feedback, so the first repair requires no synthetic operator comment.
Comment and log text is untrusted evidence, not executable shell or worker policy.
The host renders the worker prompt and exact POST body; the model chooses typed
feedback IDs, never an API body, task model, permission or executable instruction.

`repairScope` binds the host observation to the current workflow source, fixed
root, initial head and current PR head. The immutable initial head has zero
intervening commits. For a later head, the host requires a complete comparison
and verifies every commit's exact SHA, single-parent chain and complete file
inventory. Each commit must modify only `.ci-shepherd-fixture/labels.py`; changes
to tests or workflows followed by a revert still reject. A cumulative net diff
alone cannot prove preservation. See the primary
[comparison and commit APIs](https://docs.github.com/en/rest/commits/commits).

Dispatch admits zero, one or two commits beyond the initial head, with room for
exactly one labels-only fix commit. Three commits exhaust dispatch room without
preventing observation. Scope is independently refreshed before every status
mutation and the final send, before the last clock check; no GET follows that
check before POST. An initially ineligible repair spends nothing. If eligibility
is lost after a reservation or consumed publication, that capacity remains held;
there is no refund, replacement operation or POST retry. A later eligible repair
uses a new head/feedback-bound operation under the same immutable trial and
cumulative three-repair limit.

The reasoner receives at most 4096 bytes of descriptive JSON, including any
host-derived advisory. For fixture CI, the excerpt retains every unittest
case/assertion and result plus the exact repro command; timestamps, runner setup,
repeated traceback/diff and cleanup are omitted. It identifies the full
`ci-shepherd-prepare-<run>-<attempt>/envelope.json` artifact and body pointer.
The complete core packet, feedback/snapshot/history and raw envelope context
remain unchanged. Overflow fails explicitly instead of clipping either input.

Descriptive tasks retain every ID, observed state and session count, with an
exact `/context/tasks/<index>` pointer into the full context artifact. No task
is filtered out, including unknown or nonterminal work. Correlation, individual
sessions, model/usage, refs and task artifacts remain in the raw envelope for
verification; the authoritative snapshot's worker states remain unchanged.

Only a freshly verified pinned prepared-resume capability can produce the
packet/operation/trial-bound `preparedResumeAdvisory`. Existing limit checks
also apply before emitting it. It distinguishes a proven-unsent prepared intent
from reserved/consumed/uncertain work, but grants no authority. Without it, an
existing prepared intent of unknown outcome means wait. Apply independently
revalidates its default-off capability and honors a wait decision; agent
advisories or extra arguments cannot enable effects. No failed-run/source
exception or inference-budget change follows from input reduction.

Task responses can report a separate `mission_control` rate-limit resource;
ordinary `core` quota does not establish task availability. `HTTPTransport`
paces GETs from the observed remaining/reset headers, retaining the last slot
for a task POST. GET waits total at most 180 seconds per transport. A collection
that waited is discarded and collected again, with at most two full collection
attempts, so earlier head/feedback/authority reads cannot survive the wait.
Existing final guards still reject expiry, rollback, or changed authority.
POST never sleeps or retries; known exhausted admission is rejected before send.
Unexpected HTTP errors include the allowed method/path, status and sanitized
quota/reset/retry/date/request-ID headers, never credentials, response bodies,
or signed download URLs. See GitHub's
[rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

`diagnostics.py` inspects downloaded failed-run audits and comment records
offline, using explicitly supplied expected trial, operation, packet and source
identities. An audit's attempted reserved candidate is not proof of publication:
the canonical prepared record and its zero repair counter remain distinct.
The diagnostic refuses task attempts, inconsistent provenance, or an unexpected
receipt. Its output grants no recovery authority and does not relax failed
history or source-revision checks.

### Pinned prepared-intent recovery

The manual `resume_prepared` input defaults to `false`. Setting it explicitly
installs the fixed host policy in `recovery.py` for
[radical/aspire#121](https://github.com/radical/aspire/pull/121), comment
`5976480777`, and its original prepared operation and immutable trial.
It permits source migration only for observe run `37175895217` and failed run
`37176266114`, both attempt `1` at source
`a9da7a1d90195c255bfd7d91349ff0bdbbb20356`. Only these two runs receive the
prepared-intent migration authorization. Normal history sequence and
completeness checks still apply; other privileged failures remain blocked unless
they satisfy the neutral pre-apply-abort or consumed failed-apply predicates below.

Every complete observation independently downloads the named prepare, native
evidence, and receipt/failure artifacts, and verifies the pinned run, actor,
job, packet and session identities. The failed archive must have exactly three
status attempts, no task attempt and no receipt. Its persisted prepared record
must equal the authenticated current record before resumption; its attempted
reserved candidate is not spending authority. Missing artifacts, changed
feedback/head, takeover, clock rollback or expiry, and any correlated task
outcome prevent dispatch. Authenticated complete audits, including neutral
prepare observations, prevent durable budget or outcome rollback.

`PinnedRecovery` and `PreparedResume` are trusted host types, not model fields.
Prepare and apply must independently select the same policy and current run.
The closed decision contract is unchanged. Live resumption preserves the
operation ID, identity, original run/packet provenance and trial, reserves once,
and persists `consumed` before POST. It never appends a substitute operation,
starts a trial, refunds capacity, or retries a reserved/consumed/uncertain intent.
Observe remains GET-only. Local selectors cannot bypass hosted authentication.
Independent review and explicit operator deployment are required before using
this input; compilation and fake-service results do not prove a live repair.

### Authenticated pre-apply aborts

`preapply_abort.py` registers reviewed immutable source
`f16d43ba3de8ac7d299301d763d49236f1954843`; the authenticated current workflow
source also qualifies. Source registration is necessary to establish GET-only
prepare, native credential isolation and the sole gated writer. Artifact
self-claims, model fields and a skipped job alone cannot establish those facts.
This is a source-only registry, not a run allowlist or general migration policy.
Successful past privileged runs still require the normal same-source rules or
the independent two-run pinned recovery.

A neutral abort requires a completed failed manual run of the fixed workflow,
authenticated actor/repository identities, `run_attempt == 1`, and complete job
and artifact inventories. Reruns do not qualify. Exactly five jobs must be
completed: successful `prepare`, `activation` and `conclusion`, failed `agent`,
and skipped `submit_decision`
with explicit empty steps. A populated skipped-job `started_at` does not imply
executed steps. Missing proof, duplicate identities, extra
credential jobs, executed/cancelled/failed apply or failed prepare block.

Attempt 1 independently supplies provenance-bound prepare packet/envelope,
prepare audit and native collection-failure artifacts with exact ZIP members.
Closed packet, scope and recovery bindings are validated; the complete prepare
audit must have no attempts and equal the packet's authenticated canonical
record/comment. Receipts, task attempts, missing/expired/ambiguous artifacts,
unknown phases or successful native evidence cannot qualify. Failed native
output is never reused as a decision.

The witness records neutral aborted history, not a successful receipt. Prior
records retain trial/operation bindings and impose a durable floor; they grant
no new budget. Fresh current canonical/task inventories and default-off typed
prepared recovery remain independently required. Resumption preserves the
original trial and operation, honors wait, and never refunds, replaces an
intent, or retries uncertain work.

### Source-qualified consumed apply failure

`failed_apply.py` registers reviewed immutable source
`d1cb65eb4fd8b3efe456b6573c7eb04010439e36`, not a run ID or arbitrary historical
source. Its reviewed publication-before-send ordering and isolated writer are
necessary to interpret a failed audit. The independent two-run `PinnedRecovery`
witness and explicit default-off selector remain required.

Only attempt 1 of a completed failed manual run qualifies. Complete authenticated
run/job/artifact inventories must show successful `prepare`, `activation`,
`agent` and `conclusion`, and the sole failed `submit_decision` job with a failed
`Guarded host apply` step. Exact provenance-bound ZIP members independently
supply the closed original prepared packet/envelope, complete GET-only prepare
audit, successful native repair evidence, and failed apply audit/observation.
The failure must be `task PR artifact mismatch`; the audit must contain exactly
the original reserved publication, consumed publication and task-send attempt.
Missing/expired/ambiguous artifacts, successful receipts, extra attempts,
unreviewed sources, reruns or changed authority block.

This is **effect-possible** history, never a no-effect abort or successful receipt.
The consumed repair counter, original operation identity/provenance and immutable
trial impose a durable floor. Historical native success verifies prior intent
only; it cannot authorize a new POST or replace the current decision. A fresh
WAIT may use the existing guarded receipt recovery after independent current
canonical/task inventories and all normal head, feedback, management, authority
and clock checks. Confirmation requires one uniquely correlated authenticated
task. Every publication guard recomputes that unique association and requires
the same task ID; disappearance, duplication or replacement stops publication.
Live non-WAIT decisions are explicitly rejected before any mutation while the
trusted packet or fresh canonical record retains a
consumed/uncertain operation. The host does not rewrite that decision to WAIT
or route it through the shared kernel's non-WAIT receipt reconciliation.
Historical consumption remains a provenance/budget floor, not permanently
pending work after current confirmation of the uniquely authenticated task.
Missing or ambiguous outcomes retain capacity without redispatch; a
claimed confirmation without that task fails closed.

### Reviewed successful-source compatibility

`successful_history.py` registers immutable source
`9ee8070c8d5e1f6bb8a8324ad386e0e17b7fa7d5` for its consumed-to-confirmed WAIT
protocol, not a particular run. Source registration attests the reviewed code
and credential-job boundary; matching artifact shapes or a shared branch cannot
register another source. The independent two-run witness and default-off typed
recovery configuration remain required.

Only a successful manual attempt 1 from that source, exact workflow/repository
and authenticated actor qualifies. Complete job and artifact inventories must
agree with their explicit total counts. All five expected jobs and the sole
`Guarded host apply` step must succeed. Independently downloaded, provenance-bound
prepare, prepare-audit, native evidence and receipt ZIPs must contain the exact
members and bind the same packet/run/session. The native decision must be WAIT;
the audit must show exactly one confirmed publication, no task attempt and no
additional spending, preserving the original consumed operation's trial,
identity, provenance and counter.

The confirmed record becomes a durable floor. Fresh canonical authority and a
unique matching current task remain mandatory before another cycle. Another
eligible head may append an operation without altering that confirmation or
resetting the trial/budget. Unknown sources, reruns, failed writers, changed
bindings, ambiguous tasks or missing/expired evidence do not inherit this
compatibility. Historical native evidence never authorizes a new dispatch.

- Empty comments are **not** history proof. The witness independently inspects
  the sole Shepherd workflow's run attempts, jobs and downloaded host receipts.
  Its immutable creation fence is the fixed fixture's actual `created_at`, not a
  sliding lookback. Runs active at that fence cannot be excluded. Missing,
  expired, cancelled, incomplete or unrecognized failed/different-source
  privileged history blocks initialization/recovery; it never grants a new
  budget or trial.
  The workflow database ID and its two verified pre-fixture transport runs are
  pinned in `live.py`. Subsequent `run_number` gaps detect deleted attempts even
  when their status comment and task also disappear. Recreated workflows or
  incomplete sequence visibility require human recovery.
- Host-owned `audit.json` records attempted publication/dispatch before each
  write, including failure outcomes. Always-upload steps preserve failure
  diagnostics. If upload itself fails, later runs block on missing evidence.
  Canonical records and correlated tasks must agree with witnessed trial and
  operation identities. Local audit files are not authority.
- Task routes are `agents/repos/{owner}/{repo}/tasks` and `/{task_id}`, with API
  version `2026-03-10`. Both `is_archived=false` and `is_archived=true` are queried
  explicitly; the preview has returned all tasks when the filter was omitted.
  Optional aggregate counts are never completeness or live authority.
- POST uses `prompt`, `base_ref: main`, `head_ref: shepherd-fork-fixture`, and
  `create_pull_request: false`. No task model is requested. Actual identity,
  model, state, errors and usage come from server task sessions, with exact
  root/trial/operation/source-head correlation. AI-credit task usage is displayed
  in credits by dividing its nano-unit `amount` by `1e9`; AWF usage is separate.
  See the primary [task API](https://docs.github.com/en/rest/agent-tasks/agent-tasks).
- Task PR artifacts require the exact integer database ID. An omitted or blank
  `global_id` is accepted only with independent REST verification of the fixed
  PR's node/repository/ref mapping; wrong nonempty IDs and ambiguous correlation
  markers block. `task PR artifact mismatch` alone does not establish which
  metadata field differed or prove that a blank ID caused the rejection.
- A valid task POST response records diagnostic status `201` and the returned
  task ID before verification GETs. Those values are not confirmation authority.
  Delayed metadata can be reconciled only through bounded GETs of the existing
  task, never another POST. Unverifiable post-send identity retains consumed
  capacity and fails closed; sanitized verification diagnostics preserve no
  credentials or arbitrary response bodies.
- Known documented HTTP rejection is distinct from uncertainty. A lost response
  never retries POST. A later fresh wait can confirm an exactly correlated task
  receipt without spending another repair reservation. Missing/unverifiable
  tasks, unknown states, `idle`, `waiting_for_user` and `timed_out` retain capacity.
- Completion is not push/CI proof. Receipts show the actual current head and
  fixture gate on **that** head. No current-head run, approval/permission blocks,
  skipped CI or an old green remain not ready. `gate.ciPassed` describes CI
  alone; `gate.ready` also requires a head different from the initial fixture,
  a comparison changing only `labels.py`, and an authenticated completed task
  whose newest session has the exact root/trial/operation/source-head and refs.
  A newer unassociated session retains unknown worker capacity.
- A completed `action_required` PR run on the exact current head and fixture
  workflow with a complete explicit `total_count: 0` / `jobs: []` inventory is
  reported as `approval-blocked`, with `ciPassed: false` and `ready: false`.
  It supplies no repair evidence and permits read-only observation or fresh
  guarded WAIT receipt recovery, not approval, rerun or another task dispatch.
  Other missing/ambiguous jobs or unproven inventories still fail closed.
  Same-head successful `workflow_dispatch` runs do not satisfy or mask the PR
  check. GitHub's default [cloud-agent workflow approval gate](https://docs.github.com/en/copilot/concepts/security-governance-and-network-settings/risks-and-mitigations#copilot-cloud-agent-can-push-code-changes-to-your-repository)
  requires a user with write access to approve workflow execution; Shepherd
  does not approve runs or change that configuration.
- There is no proven cancellation API. Hands-off prevents new host writes and
  the worker prompt requires the same chain checks before commit/push/replies;
  observation reports any in-flight task truthfully.
- Multi-root/multi-repository installation must bind the common trial window
  and **all** reservations to common canonical remote authority before relaxing
  the singleton restriction. The callable core enforces one trusted authorized
  root; supplying another packet/root cannot hide the original reservation or
  obtain a renewed trial. It is not a global collector or multi-root scheduler.
- The hosted workflow remains the sole intended live writer, serialized by its
  existing constant concurrency group with cancellation disabled. Local runs
  cannot select live mode. No new lock service is introduced.

Keep read/mutation credentials in reviewed deterministic host jobs, never in the
reasoner. Prepare and apply must preserve the immutable trusted workflow revision
and independently downloaded same-run prepare artifact. Model-authored reports and mutable agent uploads cannot replace that boundary.
Prepare/apply obtain fresh identity directly from GitHub's TLS-authenticated OIDC
service, verifying repository, actor, hosted runner, run/attempt and workflow SHA.
Only those deterministic jobs have `id-token: write` and the selected user secret.

The generated AWF `maxAiCredits: 5` setting limits admission of further model
requests, not the total cost of an already-admitted response. AWF accounts usage
after responses; a request admitted below the threshold can finish above it.
Actual hosted observation used 9.07425 credits across two successful requests:
the first cost 4.61655, allowing the second. Record actual usage separately from
the configured threshold; do not report a hard five-credit spend cap. See the
pinned [rate-limit contract](https://github.com/github/gh-aw-firewall/blob/v0.28.20/src/types/rate-limit-options.ts)
and [credit guard](https://github.com/github/gh-aw-firewall/blob/v0.28.20/containers/api-proxy/guards/ai-credits-guard.js).
The ten-minute timeout, twelve-turn bound and disabled retries are independent
controls; neither workers nor subsequent coordinator runs share this threshold.

## Local commands

Run from the repository root:

```shell
python3 -m unittest discover -s .github/workflows/ci-shepherd/tests -p 'test_*.py' -v
python3 .github/workflows/ci-shepherd/round.py smoke \
  --workdir artifacts/ci-shepherd/run-001 \
  --workflow-sha "$(git --no-pager rev-parse HEAD)"
```

Each smoke directory must be new. The runner prepares its own packet, starts an
actual fresh Copilot subprocess, validates host session/tool evidence and the
actual final decision, then writes a no-effect receipt. `decision.json` is
diagnostic, never a manual handoff. Failed validation writes `failure.json` and
never calls apply.

Only explicitly selected `COPILOT_PROVIDER_*` inference variables and
`COPILOT_MODEL` are forwarded. Set a scoped provider endpoint and credential in
the calling environment; never use a GitHub task/publication token for this
runner. The child gets a new home/config directory, no user instructions, no
built-in MCP servers, and no tools or file-read permissions: the packet is already
in the prompt. Any attempted tool request, execution, or completion fails
validation before apply. Provider credential commands and inherited worker secrets
are rejected. The launcher does not read the user's Copilot or gh configuration.

**Local runtime proof is blocked on Copilot 1.0.92-3.** Its native credit flag
rejects the requested five-credit limit with:

```text
error: Invalid value for --max-ai-credits: "5". Use at least 30 AI credits.
```

The launcher fails closed rather than raising that budget silently. The native
CLI has no verified twelve-turn enforcement here. Subprocess fixtures validate
the boundary, not real inference. An isolated mock-provider probe verified the
1.0.92-3 session, final-message, effective-tool fingerprint, and MCP call formats;
it is not a successful inference or hosted proof.

## Manual hosted workflow

Compile with the repository-pinned **v0.89.17** compiler, not a newer global
extension. An isolated release binary can be kept under
`artifacts/ci-shepherd/tooling/gh-aw` after verifying its release asset digest:

```shell
artifacts/ci-shepherd/tooling/gh-aw version
artifacts/ci-shepherd/tooling/gh-aw compile ci-shepherd --validate --no-check-update
artifacts/ci-shepherd/tooling/gh-aw lint .github/workflows/ci-shepherd.lock.yml --shellcheck
artifacts/ci-shepherd/tooling/gh-aw compile ci-shepherd --validate --no-check-update
```

Commit the generated lock with its Markdown source; do not edit the lock.
Recompilation must not change the generated file. The compiled workflow is
manual-only and uses one constant workflow concurrency group without cancelling
an active run. No deployment or hosted run is implied by local compilation.

The pinned lint command requires Docker. Without a running daemon, use the
installed actionlint with the same two compatibility exclusions needed here:

```shell
actionlint -shellcheck shellcheck \
  -ignore 'unknown permission scope "copilot-requests"' \
  -ignore 'unexpected key "queue" for "concurrency" section' \
  .github/workflows/ci-shepherd.lock.yml
```

The hosted engine uses native Copilot routing (`copilot-requests: write` and
`github.token`); it receives no user task token. The pinned compiler emits a
ten-minute execution timeout and AWF proxy budgets of five credits and twelve
turns. Harness retries are disabled. These are supported configuration bounds,
not a claimed twenty-tool-call limit or evidence of hosted enforcement.

The execution step explicitly empties `GH_TOKEN`, `GITHUB_TOKEN`,
`GH_AW_GITHUB_TOKEN`, `GH_AW_GITHUB_MCP_SERVER_TOKEN`, and
`CI_SHEPHERD_USER_TOKEN` through `engine.env`.
Empty-string GitHub expressions keep these bindings valid in v0.89.17; literal
empty strings are emitted as YAML null by that compiler.
The native inference token remains scoped to the AWF API proxy; it is not a
user task or publication credential. AWF's `--env-all` cannot inherit nonempty
values for the four emptied keys.

## Trust boundary

`round.py` owns the versioned packet, random packet ID/nonce, closed decision
schema, expected repository/run/attempt/workflow revision, and guarded receipt.
`reasoning.py` validates host-produced events and the actual model tool
fingerprint. Missing reports, resumed sessions, extra grants, unauthorized calls,
duplicate decisions, mismatches, or unsuccessful processes fail closed.

Prepare and apply check out the immutable `github.workflow_sha`, never target PR
code. Prepare uploads only `packet.json` and the host-owned `envelope.json`.
Apply independently downloads that same-run, same-attempt artifact. Agent
identity claims cannot replace it.

The hosted reasoner receives the packet in its prompt and can call only
`safeoutputs-submit_decision`. Its automatically created session must be fresh.
Trusted post-processing records the actual session, tool fingerprints, call
completion, and final decision; it does not accept an agent-authored permission
report. Apply validates that evidence again against the actual compiler-managed
safe-output item. It never executes agent-uploaded code or configuration.

Only the required preparation, host-evidence, and receipt/failure files cross
the custom artifact boundary. `engine.env.AWF_SESSION_STATE_DIR` selects
`artifacts/ci-shepherd/session-state` under the runner workspace. Trusted
post-processing reads that same host directory, not the runner's Copilot home.
AWF supports the [environment override](https://github.com/github/gh-aw-firewall/blob/v0.28.20/src/commands/build-config.ts#L169)
and mounts it over the container's Copilot session state; see pinned
[path resolution](https://github.com/github/gh-aw-firewall/blob/v0.28.20/src/log-paths.ts)
and [home mounts](https://github.com/github/gh-aw-firewall/blob/v0.28.20/src/services/agent-volumes/home-strategy.ts).

Behavioral tests execute the generated collection command against fixture
session files and capture the generated AWF launch environment with sentinel
worker credentials. They do not prove a real container mount or inference.
These fixture assertions alone do not prove a real hosted collection or native
MCP transport. Failure to find the expected host reports prevents a receipt.

## Parent-operated first live gate

Review and deploy only this workflow, its generated lock and the
`ci-shepherd/` directory to the fork's registered workflow branch. Keep the
fixture branch separate and never check it out with the host credential.
Recompile with the pinned tool before deployment. The new restricted secret
requires compiler security review; the manual-dispatch concurrency warning is
intentional because one constant workflow group serializes all modes.

The parent provisions `CI_SHEPHERD_USER_TOKEN` securely in `radical/aspire` for
the selected `radical` user. The primary task API requires a supported user
credential with agent-task read/write access; installation tokens are unsupported.
The host also needs issue-comment write, repository/PR read, and Actions read.
The secret is referenced only in deterministic prepare/apply steps and is never
part of a packet, prompt, artifact or reasoner environment.

After reviewing the worker/status templates, the parent adds `shepherd-adopted`
to **PR 121 only**. Without adoption the gate stops before reasoning/effects.
These commands are the parent's operations, not actions performed by local tests:

```shell
gh workflow run ci-shepherd.lock.yml --repo radical/aspire \
  --ref <reviewed-deployment-branch> -f mode=observe
gh run watch <observe-run-id> --repo radical/aspire --exit-status
gh workflow run ci-shepherd.lock.yml --repo radical/aspire \
  --ref <same-reviewed-deployment-branch> -f mode=live
gh run watch <live-run-id> --repo radical/aspire --exit-status
```

Inspect the independent prepare/evidence/receipt artifacts, not just green job
conclusions. Observe must show the initial SHA and failed fixture assertion logs,
fresh session/tool evidence, zero effects and no trial/status comment. The first
live repair must show persisted prepared/reserved/consumed authority, one
cumulative repair, and one actual task ID on the existing branch—never a new PR.
The actual server-selected model is recorded, not assumed.

Run another fresh live cycle to observe progress: an active task must produce a
wait with no second task POST. On a changed head, the old failure/success cannot
substitute for the new fixture CI. Check the worker changed only `labels.py`,
preserved the ten fixture cases, and reported its commit/test/CI evidence.
Any approval/licensing/permission block, uncertain receipt or incomplete history
requires human recovery, not redispatch or renewed authority.

Local stdlib tests exercise fake HTTP and generated prepare/collector/apply
commands, including delayed receipt recovery and hostile log text remaining
data. They do **not** establish a real cloud task launch, verified worker push,
CI approval, cancellation, OIDC service compatibility or an end-to-end live
trial. Those remain the parent's actual service gate.
