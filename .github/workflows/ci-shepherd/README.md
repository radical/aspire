# CI Shepherd no-effect transport

This slice proves a closed packet/decision/receipt boundary. It has no issue or PR
repair lanes, remote workers, scheduling, cancellation, publication, or live mode.
A receipt always records `outcome: wait` and an empty `effects` array.

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
Hosted collection and native MCP transport remain unproven until a real run.
Failure to find the expected host reports prevents a receipt.
