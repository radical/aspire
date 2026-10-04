# CI Shepherd guarded fixture gate

The manual workflow defaults to `transport-proof`: a fresh native decision and a
receipt with `outcome: wait` and empty `effects`. `observe` performs deterministic
GET-only collection and fresh reasoning without starting a trial. `live` installs
one capability: repairing the original label-normalization defect in
`radical/aspire#121` on the existing `shepherd-fork-fixture` branch targeting `main`.
Only `.ci-shepherd-fixture/labels.py` may change; fixture assertions must not.

`hosted.py` prepares host-owned artifacts and applies independently downloaded
same-run inputs. `live.py` implements the fixed REST adapter, independent history
witness and existing-PR executor. `policies/pr.md` is the packet-first reasoning
and output policy. No issue lane, subsequent defect repair, rerun, checkpoint
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
`a9da7a1d90195c255bfd7d91349ff0bdbbb20356`. No other failed attempt or historical
source receives an exception. Normal history sequence and completeness checks
still apply; later current-source attempts must satisfy the normal success rules.

Every complete observation independently downloads the named prepare, native
evidence, and receipt/failure artifacts, and verifies the pinned run, actor,
job, packet and session identities. The failed archive must have exactly three
status attempts, no task attempt and no receipt. Its persisted prepared record
must equal the authenticated current record before resumption; its attempted
reserved candidate is not spending authority. Missing artifacts, changed
feedback/head, takeover, clock rollback or expiry, and any correlated task
outcome prevent dispatch. Complete current-source audits prevent durable budget
or outcome rollback.

`PinnedRecovery` and `PreparedResume` are trusted host types, not model fields.
Prepare and apply must independently select the same policy and current run.
The closed decision contract is unchanged. Live resumption preserves the
operation ID, identity, original run/packet provenance and trial, reserves once,
and persists `consumed` before POST. It never appends a substitute operation,
starts a trial, refunds capacity, or retries a reserved/consumed/uncertain intent.
Observe remains GET-only. Local selectors cannot bypass hosted authentication.
Independent review and explicit operator deployment are required before using
this input; compilation and fake-service results do not prove a live repair.

- Empty comments are **not** history proof. The witness independently inspects
  the sole Shepherd workflow's run attempts, jobs and downloaded host receipts.
  Its immutable creation fence is the fixed fixture's actual `created_at`, not a
  sliding lookback. Runs active at that fence cannot be excluded. Missing,
  expired, cancelled, failed, incomplete or different-source privileged history
  blocks initialization/recovery; it never grants a new budget or trial.
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
