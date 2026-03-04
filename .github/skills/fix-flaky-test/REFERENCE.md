# Fix-Flaky-Test Reference

Lookup material for the fix-flaky-test skill. Consult specific sections when SKILL.md directs you to.

## § Verification Scale

Based on failure rate, calculate iterations for **95% probability of seeing at least one failure** (`n ≥ log(0.05) / log(1-p)`):

| Failure Rate | Runners × Iterations per OS | Total per OS | Confidence |
|---|---|---|---|
| >50% | 3 × 3 | 9 | >99% |
| 20-50% | 5 × 5 | 25 | >99% |
| 10-20% | 5 × 10 | 50 | >99% |
| 5-10% | 10 × 10 | 100 | >99% |
| <5% | 10 × 25 | 250 | >95% |

**Defaults:** `configure-reproduce.ps1` uses 3 runners × 10 iterations, which covers failure rates down to ~20% at 95% confidence. Override with `-Runners` / `-Iterations` for lower rates.

**Local iteration counts** (single machine, no parallelism):

| Failure Rate | Local Iterations | Expected failures |
|---|---|---|
| >50% | 10 | ~5+ |
| 20-50% | 20 | ~4-10 |
| 10-20% | 30 | ~3-6 |
| 5-10% | 50 | ~2-5 |
| <5% | 100 | ~1-5 |

**CI verification scale by confidence level:**

| Original Failure Rate | High Confidence | Low Confidence |
|---|---|---|
| >50% | 3 × 3 per OS (9) | 3 × 3 per OS (9) |
| 20-50% | 3 × 5 per OS (15) | 5 × 5 per OS (25) |
| 10-20% | 5 × 5 per OS (25) | 5 × 10 per OS (50) |
| 5-10% | 5 × 10 per OS (50) | 10 × 10 per OS (100) |
| <5% | 10 × 10 per OS (100) | 10 × 25 per OS (250) |

High confidence = root cause matches known pattern, mechanical fix, local reproduction succeeded.
Low confidence = hypothesis-based, behavioral changes, no local reproduction.

## § CI Architecture

### reproduce-flaky-tests.yml Workflow

The workflow has three jobs:

1. **Setup job** (`Generate matrix`): Parses env vars, generates a matrix of `{os, index}` combinations.
2. **Reproduce jobs** (parallel): Each runner builds the test project once, then loops through iterations with DCP process cleanup between runs.
3. **Results job** (`Reproduce Results`): Aggregates pass/fail across all runners into a summary table.

Failed iterations upload test output as artifacts named `failures-<os>-<index>`.

The workflow automatically disables the quarantine exclusion filter (via `/p:_NonQuarantinedTestRunAdditionalArgs=""`), so quarantined tests are included. You do NOT need special flags.

### workflow_dispatch Behavior

`workflow_dispatch` requires the workflow file to exist on the **default branch** (`main`). Key implications:

- Dispatch against any branch with `gh workflow run reproduce-flaky-tests.yml --ref <branch>`. GitHub discovers the workflow from `main` but runs the version from the specified `--ref`, so your env var edits are used.
- Creating a new workflow file on a feature branch won't work — GitHub won't discover it via `workflow_dispatch` until merged to `main`.
- The investigation branch has `ci.yml` disabled, so pushes don't trigger full CI.

### Test project shortname mapping

The workflow resolves `TEST_PROJECT` to a path:
- Tries `tests/{name}.Tests/{name}.Tests.csproj` first
- Then `tests/Aspire.{name}.Tests/Aspire.{name}.Tests.csproj`
- Examples: `Hosting` → `Aspire.Hosting.Tests`, `Hosting.Azure` → `Aspire.Hosting.Azure.Tests`

## § Build System Quarantine Filtering

`eng/Testing.props` auto-appends `--filter-not-trait "quarantined=true"` to test arguments via the `TestRunnerAdditionalArguments` MSBuild property. This is evaluated during `dotnet test` even with `--no-build`.

- **CI reproduce workflow**: Overrides `_NonQuarantinedTestRunAdditionalArgs` to empty, removing the filter for all tests.
- **Local reproduction**: Pass `/p:RunQuarantinedTests=true` to both `dotnet build` and `dotnet test`.

`Testing.props` also adds `--ignore-exit-code 8`, which masks zero-test runs as successes. The `run-test-repeatedly` scripts and the reproduce workflow detect this by checking for the `Total:` count indicator.

## § Windows Encoding

Windows CI log files downloaded as artifacts are encoded as **UTF-16LE**. Running `cat` on them produces garbled output. Convert first:

```bash
iconv -f UTF-16LE -t UTF-8 /tmp/failure-logs/failures-windows-latest-1/test-output.log > /tmp/readable-windows.log
```

**Tip**: Using `gh api "repos/dotnet/aspire/actions/jobs/<job_id>/logs"` returns UTF-8 directly, avoiding encoding issues. Prefer API-based log retrieval when possible.

## § Contention Indicators

A test is likely **contention-sensitive** (fails only alongside other tests) if:

1. It uses `randomizePorts: false` — fixed ports conflict with concurrent tests
2. It uses a shared fixture (collection or class fixture) — startup timing depends on other tests
3. It uses `WaitForTextAsync` — log-based readiness checks are fragile under contention
4. It shares a `CancellationTokenSource` across startup and readiness phases
5. The tracking issue shows 0% failure on macOS but failures on Linux/Windows

If contention-sensitive, single-test CI reproduction may fail — escalate to quarantine-project mode.

## § OS Targeting

- **High failure rate (>20%) on one OS**: Target that OS only — fastest feedback
- **High rate on multiple OSes**: Target all failing OSes
- **Low rate or unknown**: Target `ubuntu-latest,windows-latest` with moderate iterations
- **Docker-dependent tests** (`[RequiresFeature(TestFeature.Docker)]`): Standard Windows CI runners do NOT have Docker. Target only `ubuntu-latest` (and `macos-latest` if needed).

## § Flaky Test Patterns

See `.github/instructions/test-review-guidelines.instructions.md` for the comprehensive patterns table. Key patterns:

| Pattern | Fix |
|---------|-----|
| Thread-unsafe collections in test fakes | `lock` or `ConcurrentBag<T>` |
| `WaitForTextAsync` for readiness | `WaitForHealthyAsync()` |
| Shared CancellationTokenSource | Separate CTS per phase |
| Sequential service waits | `Task.WhenAll` |
| `randomizePorts: false` | `randomizePorts: true` |
