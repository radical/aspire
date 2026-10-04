# CI Shepherd guarded core and no-effect transport

The installed workflow proves a closed packet/decision/receipt boundary. Its
transport-proof receipt always records `outcome: wait` and an empty `effects`
array. The separate reconciliation library supplies guarded state transitions
and remote receipt recovery, exercised with closed host snapshots and fake
effects. No repair, assignment, adoption, rerun or checkpoint handler is
installed. No CLI command selects live reconciliation.

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

### Collector and live-installation boundary

`github.py` is an injectable transport primitive, not a credential loader or
complete snapshot collector. Its reads are allowlisted to issue/PR identity and
issue-comment inventories in one configured repository. Its only write primitive
is a guarded host-rendered root status comment. Paging errors, foreign links,
skipped/cyclic pages and pagination-limit exhaustion are explicit failures;
a full page without a next link requires another probe. See the primary
[comment API](https://docs.github.com/en/rest/issues/comments) and
[pagination contract](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).

The host normalization/collection lane is not installed. In particular:

- An empty comment list does **not** establish complete prior-chain history.
  `history` must independently exclude prior publication attempts, record IDs
  and associated operations/workers before bootstrap. A deleted or unverifiable
  previously created record requires human recovery. The fake history witness
  is not a claim that GitHub task aggregate counts or comment absence prove this.
- Issue revisions must bind all relevant issue inputs; PR feedback collection
  must be complete and revision-bound. Actual task API requests and verifiable
  root/operation correlation are required before installing worker handlers.
- The current Task collection route is `agents/repos/{owner}/{repo}/tasks`.
  Its response requires `tasks`, not aggregate counts, and its default
  `is_archived=false` excludes archived tasks. `workersArchived` and
  `workersUnarchived` completeness must each be established by host evidence
  from the corresponding lane; otherwise observation is incomplete and apply
  blocks. Archiving alone never makes a task terminal or frees owned capacity.
  See the primary
  [API schema](https://github.com/github/rest-api-description/blob/main/descriptions/api.github.com/api.github.com.json).
  This library does not install Task collection or POST handlers.
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
and independently downloaded same-run prepare artifact. Model-authored reports
and mutable agent uploads cannot replace that boundary. The current workflow
does not grant this library new permissions or invoke live handlers.

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
`github.token`); it needs no user task token. The pinned compiler emits a
ten-minute execution timeout and AWF proxy budgets of five credits and twelve
turns. Harness retries are disabled. These are supported configuration bounds,
not a claimed twenty-tool-call limit or evidence of hosted enforcement.

The execution step explicitly empties `GH_TOKEN`, `GITHUB_TOKEN`,
`GH_AW_GITHUB_TOKEN`, and `GH_AW_GITHUB_MCP_SERVER_TOKEN` through `engine.env`.
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
