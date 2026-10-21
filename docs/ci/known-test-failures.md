# Experimental known-test-failure replay

`tools/KnownTestFailures` is a standalone .NET 10 tool for replaying recorded
managed-test results against exact, approved failure allowances. It does not
change test attributes, raw results, or production CI outcomes. An isolated
fork-only workflow exercises a fixed fixture; there is no production enforcement
mode or general-purpose workflow adapter.

## Modes and exits

- `off` is the default. It returns the original exit without opening registry,
  manifest, or TRX inputs, even when their paths or timestamp are invalid.
- `shadow` reports `pass`, `allowed`, or `blocked` but always preserves the
  original exit, including when inputs are invalid.
- `experimental` requires `--opt-in` and an `experimental` registry. A valid
  complete passing run or fully allowed failing run returns zero. Blocked
  evaluation preserves a nonzero original exit, or returns evaluator code `3`
  when the original exit was zero. Report-write errors follow the same rule.

Reports distinguish `originalExitCode` from `effectiveExitCode`. Active
evaluation requires explicit `--at` UTC time; no implicit expiry duration or
maximum lifetime is imposed. `--report` creates a new file exclusively and will
not overwrite evidence or existing reports.

```bash
dotnet run --project tools/KnownTestFailures -- evaluate --original-exit 2

dotnet run --project tools/KnownTestFailures -- evaluate \
  --mode shadow --original-exit 2 --at 2026-10-20T12:00:00Z \
  --registry registry.json --evidence evidence.json --artifact-root artifacts

dotnet run --project tools/KnownTestFailures -- evaluate \
  --mode experimental --opt-in --original-exit 2 --at 2026-10-20T12:00:00Z \
  --repo . --ref refs/remotes/origin/experimental-allowances \
  --registry-path registry.json --evidence evidence.json \
  --artifact-root artifacts --report decision.json
```

For `evaluate`, the caller fetches the desired remote ref separately.
The isolated `gate` command instead requires its own successful fresh fetch.
Neither command pushes, switches branches, or executes branch contents.
A branch read rejects symbolic refs, resolves a full `refs/heads/` or
`refs/remotes/` ref once, then reads `commit:path`. The report records that
commit, blob ID, and path.
Local-file provenance is explicitly `local-untrusted`; reading a local git ref
also does not establish protected-branch admission.

## Registry schema and lifecycle

The source of truth is an append-only event log, not a mutable snapshot.
Schema version `1` uses case-sensitive camelCase names. Unknown or duplicate
properties, null values, missing required fields, invalid history, unsupported
versions/environments, and ambiguous overlapping active matches are rejected.
Optional `content` must be omitted, not null.

```json
{
  "schemaVersion": 1,
  "environment": "experimental",
  "history": [
    {
      "sequence": 1,
      "id": "example-case-allowance",
      "action": "add",
      "actor": "maintainer",
      "reason": "Reproduced the linked failure",
      "at": "2026-10-19T12:00:00Z",
      "contentRevision": 1,
      "content": {
        "project": "tests/Example.Tests/Example.Tests.csproj",
        "method": "Example.Tests.Flaky",
        "displayName": "Flaky(value: 1)",
        "caseId": "trusted-stable-case-1",
        "lane": "regular",
        "platform": "linux-x64",
        "ref": "refs/heads/main",
        "message": "known error",
        "issue": "https://example.com/issues/123",
        "owner": "example-team",
        "createdAt": "2026-10-19T12:00:00Z",
        "expiresAt": "2026-10-22T12:00:00Z"
      }
    },
    {
      "sequence": 2,
      "id": "example-case-allowance",
      "action": "approve",
      "actor": "reviewer",
      "reason": "Approved this exact content revision",
      "at": "2026-10-19T13:00:00Z",
      "contentRevision": 1
    }
  ]
}
```

The issue above is a schema example, not a real tracked failure.

| Action | Rules |
| --- | --- |
| `add` | Unused stable ID, content revision `1`, full content |
| `update` | Live active entry, full replacement content, revision incremented by exactly one; preserves `createdAt` and invalidates approval |
| `approve` | Current revision, no content; records actor as approver and event time as approval time |
| `revoke`, `remove`, `expire` | Current revision, no content; terminal tombstone, retained in history |

Event sequences are contiguous from `1`; timestamps are nondecreasing absolute
UTC instants. Every event requires nonempty actor/reason. Content changes include
owner, issue, expiry, identity, signature, and scope. Approval does not increment
the content revision. Evaluation requires
`createdAt <= approvedAt <= evaluatedAt < expiresAt`; future history blocks.
Revoked, removed, manually expired, or naturally expired allowances cannot be
renewed by update. Add a new ID instead; removed IDs are never reusable.

All identity, scope, and message comparisons are ordinal and exact. Project paths
must be normalized repository-relative `.csproj` paths. Refs must be full local or
remote branch refs. The initial signature is the nonempty `ErrorInfo.Message`
only; stack traces are preserved in raw TRX, not used for matching. Regexes,
globs, message normalization, and guessed parameter parsing are unsupported.

### Local branch management

Management stores `registry.json` at the branch root. `--event` takes one event
object in the format above, with the next sequence and explicit subject revision.
The following commands use a local branch, not a remote registry:

```bash
dotnet run --project tools/KnownTestFailures -- mutate \
  --repo . --ref refs/heads/experimental-allowances \
  --expected 0000000000000000000000000000000000000000 \
  --initialize --event add-event.json

dotnet run --project tools/KnownTestFailures -- mutate \
  --repo . --ref refs/heads/experimental-allowances \
  --expected FULL_COMMIT_FROM_PREVIOUS_COMMAND --event approve-event.json
```

Replace `FULL_COMMIT_FROM_PREVIOUS_COMMAND` with the emitted commit ID.
Use an all-zero ID of the repository's object-ID width only for explicit
initialization of an absent branch. Existing branches require their exact full
expected commit. Git `hash-object`, `mktree`, `commit-tree`, and
`update-ref --no-deref <ref> <new> <expected>` create a reviewable commit and
perform compare-and-swap. Competing updates fail instead of overwriting.
Other branch-tree files are retained; checkout, index, and working files are
untouched. Symbolic registry refs are rejected. Failed CAS can leave unreachable
objects for normal Git garbage collection. No custom index or temporary checkout
is created. Management rejects branches checked out in any worktree.

Every Git child overrides `core.hooksPath` with the running executable's path,
an existing non-directory file. Hook discovery cannot find child files beneath
it, including `reference-transaction` hooks during `update-ref`. This disables
repository and configured hooks without platform-specific device paths,
temporary files, or repository/global configuration changes.

See [Git update-ref](https://git-scm.com/docs/git-update-ref) for expected-old
object and symbolic-ref semantics, and
[core.hooksPath](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corehooksPath)
for the per-command hook override.

## Trusted evidence manifest

The manifest must come from an independently trusted producer that records
**all selected managed invocations** and the **same filtered discovery inventory**
used to execute each one. A user-supplied manifest can lie; this replay tool
cannot authenticate discovery, process completion, or approvals.

```json
{
  "schemaVersion": 1,
  "lane": "regular",
  "platform": "linux-x64",
  "ref": "refs/heads/main",
  "selectedInvocations": ["example-tests"],
  "invocations": [
    {
      "id": "example-tests",
      "project": "tests/Example.Tests/Example.Tests.csproj",
      "trxPath": "example/results.trx",
      "directMtpProcess": true,
      "processExitCode": 2,
      "setupSucceeded": true,
      "completedNormally": true,
      "diagnostics": [],
      "expectedCases": [
        {
          "caseId": "trusted-stable-case-1",
          "testId": "run-specific-trx-test-id",
          "method": "Example.Tests.Flaky",
          "displayName": "Flaky(value: 1)"
        }
      ]
    }
  ]
}
```

Each `caseId` is an opaque **stable framework case identity**, not the display
name or a generated hash of its text. The producer must prove its stability
across runs and provide an unambiguous per-run binding to TRX `testId`, exact
method, and exact display. Equal custom `ToString()` output or display truncation
must not merge parameter cases. Existing MTP TRX IDs are **not** assumed
universally stable. If stability or mapping cannot be established, the producer
must reject the run; this tool does not infer missing identity.

Inventories must be nonempty, with unique case IDs per project and unique TRX
test IDs per invocation. Selected invocation IDs and invocation records must be
equal one-to-one; artifacts cannot be duplicated. Discovered cases and actual
TRX results must be exactly equal, including definition linkage. Counters are
checked for consistency but never substitute for discovery completeness.

Only direct MTP test-process exit `0` with no failures, or `2` with test failures,
is accepted. Every other process code, wrapper-only exit, setup failure, crash,
diagnostic, abnormal completion, missing artifact, or inconsistent aggregate exit
blocks evaluation. An aggregate zero with project failures is inconsistent.
The only supported original aggregate contract is `0` for a complete passing
run and `2` for a complete test-failing run. Other aggregate exits, including
`9` and `137`, remain blocked even when every recorded test failure matches an
allowance; no wrapper-exit translation is inferred.
See [MTP exit codes](https://learn.microsoft.com/dotnet/core/testing/microsoft-testing-platform-troubleshooting#exit-codes).

The conservative TRX slice supports concrete `Passed` and `Failed`
`UnitTestResult` records with linked `UnitTest` definitions in the TeamTest 2010
namespace and a normal run summary. DTDs are disabled. Skipped, timed-out,
aborted, error, aggregate/nested/extended results, malformed messages, run-level
`ErrorInfo`, and `RunInfos` diagnostics block. Each failed case must match exactly
one currently eligible approval; mixed known/new failures remain red.
TRX alone does not prove completion:
[MTP report streaming](https://learn.microsoft.com/dotnet/core/testing/microsoft-testing-platform-test-reports)
can leave partial reports after a crash.

Raw files are not modified. The decision includes consumed registry JSON,
provenance, evaluation time, the parsed manifest with all original process exits,
raw TRX for artifacts read before a decision/error, matched IDs/revisions, and
blocking diagnostics. Keep original manifests, reports, artifacts, and process
diagnostics together. Reports include failure text and should inherit the
original results' access controls.

## Deferred admission and integration policy

Before any real CI adapter, separately decide approval admission authority,
maximum lifetime, the trusted stable-case discovery/completion producer, protected
registry branch ownership, job-end integration, retroactive evaluation, old-branch
behavior, and how expiring allowances affect stale green checks. Actor/reason
strings and approval metadata are recorded assertions, **not authentication**.
This tool neither establishes those authorities nor refreshes existing checks.

The tool is included in `Aspire.slnx` and referenced by `Infrastructure.Tests`.
Its compiled inputs are tracked by the normal ProjectGraph. The intentionally
failing fixture is outside the solution and normal test matrices.
`eng/github-ci/test-trigger-map.yml` routes its entire dedicated directory to
`Infrastructure.Tests`, which builds and runs it at runtime. The proof workflow
is a dedicated experimental lane, not a selector-gated production job; the
existing workflow routing classifies it for infrastructure coverage.

## Isolated real-framework proof

`.github/workflows/known-test-failures-proof.yml` runs **only in
`radical/aspire`**, on pushes to `known-test-failure-allowances` and same-fork
draft PRs whose head is that branch. It checks out the actual PR head SHA,
not GitHub's merge ref, and records the event, executed SHA, and declared test
scope. Jobs use read-only permissions, no secrets, no persisted checkout
credentials, and no `pull_request_target`. No production workflow is changed.

The `prove` job asserts all expected outcomes and stays green when the experiment
works. The `gate` job returns the evaluator's actual effective exit:

```text
real fixture → raw MTP exit 2 → unapproved registry → effective exit 2 (red)
same head    → raw MTP exit 2 → approved registry   → effective exit 0 (green)
```

Every gate run freshly fetches
`refs/heads/known-test-failures-registry-proof` from the fork into
`refs/remotes/known-failure-proof/registry`. A failed fetch blocks even when
that tracking ref already contains an approved revision. Only the branch-root
`registry.json` is consumed, as data. Raw registry JSON and commit/blob provenance
are saved before validation. Missing or malformed data cannot waive a failure.
The live gate uses actual UTC time, lane `experimental-proof`, the runtime RID
(`linux-x64` on the proof runner), and exact test scope
`refs/heads/known-test-failure-allowances`.

### Fixture identity and completion contract

`tools/KnownTestFailuresFixture` contains exactly three nonparameterized facts:
`KnownTestFailuresFixture.Cases.Pass`, `.Candidate`, and `.Neighbor`. Ordinary
execution without `KNOWN_FAILURE_PROOF_OPT_IN=true` passes. Only the typed runner
supplies the fixed failure-injection scenarios.

Before execution, the runner calls the compiled MTP executable's
`--list-tests json` twice in separate processes. It checks schema version `1`,
the authored methods, displays, nonparameterized shape, unique framework UIDs,
and identical discovery across both processes. It persists this independent
inventory **before** launching execution.

xUnit's supported `.uniqueid` sidecar gives this fixture a fixed assembly
identity instead of the default absolute assembly-path identity. The framework
derives collection/class/method/case IDs from that identity. The proof also
copies the built fixture to a different directory and verifies identical UIDs.
See the pinned framework's
[assembly identity implementation](https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.common/Utility/UniqueIDGenerator.cs)
and [reflection case identity](https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.common/Utility/UniqueIDGenerator_reflection.cs).
These are real framework UIDs, not hashes invented from display names.
This contract does **not** establish identity for arbitrary tests, theories,
custom displays, or different framework versions. Compare Linux artifacts with
the seeded UID before approval; any difference must block, not fall back to names.

Execution launches the compiled MTP apphost directly, not `dotnet test`.
It captures raw exit, stdout, stderr, xUnit TRX (`--report-xunit-trx`), and
xUnit lifecycle XML (`--report-xunit-xml`). The xUnit TRX writer avoids the
platform TRX writer's additional run-level exit diagnostic; run diagnostics
remain unsupported by the strict parser. Optional `TestEntries` must link every
result to the same definition execution and list.

The lifecycle report must contain an assembly finish marker, zero framework
errors, no skips/not-run results, exact totals, and a unique finished test with
the same method, display, outcome, and message for every TRX result.
`completedNormally` is derived from these framework lifecycle checks, not set
unconditionally or inferred from a console success string.
The per-run TRX GUID is bound to pre-execution discovery only through a unique
exact method/display match. These simple fixed facts make that mapping valid;
no general parameterized mapping is claimed.

See [MTP options](https://learn.microsoft.com/dotnet/core/testing/microsoft-testing-platform-options)
and the [xUnit lifecycle report writer](https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.runner.common/Results/Builtin/XmlV2ResultWriterMessageHandler.cs).

### Local commands and branch experiment

After the repository SDK restore, build the fixture separately and run the
typed proof entry point. Output directories must not already exist.

```bash
dotnet build tools/KnownTestFailuresFixture/KnownTestFailuresFixture.csproj
dotnet run --project tools/KnownTestFailures -- prove \
  --repo "$PWD" \
  --fixture "$PWD/artifacts/bin/KnownTestFailuresFixture/Debug/net11.0/KnownTestFailuresFixture" \
  --output "$PWD/artifacts/known-failure-proof"
```

The suite proves passing, unapproved, approved-only, mixed known/new, changed
signature, wrong scope, updated-without-reapproval, reapproved, exact expiry
boundary, revoked, removed, missing/malformed registry, off, and shadow cases.
Valid registry scenarios exercise local branch mutation and fresh branch
consumption in isolated fixture repositories, never the user's refs. Boundary
cases use the explicit synthetic clock recorded in their decisions, not a
production TTL policy. Relocation checks the fixed framework UID contract.

Seed an **unapproved** event from repeated actual discovery. Supply an explicit
several-hour experiment expiry and a real tracking link; no default TTL is chosen.
The `seed` command writes files only and does not mutate a branch.

```bash
dotnet run --project tools/KnownTestFailures -- seed \
  --repo "$PWD" \
  --fixture "$PWD/artifacts/bin/KnownTestFailuresFixture/Debug/net11.0/KnownTestFailuresFixture" \
  --output "$PWD/artifacts/known-failure-seed" \
  --expires "$PROOF_EXPIRES_UTC" --issue "$PROOF_ISSUE" --owner "$PROOF_OWNER" \
  --platform linux-x64

dotnet run --project tools/KnownTestFailures -- mutate \
  --repo . --ref refs/heads/known-test-failures-registry-proof \
  --expected 0000000000000000000000000000000000000000 --initialize \
  --event artifacts/known-failure-seed/add.json
```

An operator can publish that data branch separately, then rerun the unchanged
head's gate before and after appending an `approve` event with `mutate`.
Approval identifies `proof-candidate`, sequence `2`, content revision `1`, an
explicit actor/reason, and actual UTC `at`, with no `content`. `update` uses the
next sequence, increments the content revision, preserves `createdAt`, and
carries full changed content; reapproval must name that new revision. `revoke`
or `remove` uses the current revision without content. Each mutation requires
the exact previous commit from `mutate`, as in the management commands above.
Actor strings are **test-only assertions**, not permission checks.

```bash
dotnet run --project tools/KnownTestFailures -- gate \
  --repo "$PWD" \
  --fixture "$PWD/artifacts/bin/KnownTestFailuresFixture/Debug/net11.0/KnownTestFailuresFixture" \
  --output "$PWD/artifacts/known-failure-gate" \
  --remote https://github.com/radical/aspire.git
```

Do not wrap this command with success-forcing shell logic or `continue-on-error`.
The workflow always uploads discovery, inventory, raw process exits,
stdout/stderr, TRX, lifecycle XML, evidence, decisions, and fetch provenance,
including artifacts from red gates. A branch-only approval changes the registry
revision, not the executed head. Expiry does not retroactively invalidate a
previously green GitHub check; rerun the gate to evaluate current policy.
