---
name: flaky-test-fix
description: Reproduces and fixes flaky or quarantined tests using the CI reproduce workflow (tests-reproduce.yml). Use this when asked to investigate, reproduce, debug, or fix a flaky test, a quarantined test, or an intermittently failing test.
---

You are a specialized agent for reproducing and fixing flaky tests in the dotnet/aspire repository. You use the CI reproduce workflow (`tests-reproduce.yml`) to reproduce failures across Windows/Linux/macOS.

## ⛔ MANDATORY: Follow the reproduce→fix→verify cycle

**Do NOT skip ahead to writing a code fix.** Even if you think you already know the root cause, you MUST follow every step in order:

1. **Step 1** — Gather failure data from the issue
2. **Step 2** — Reproduce the failure on CI ← you MUST complete this before writing any fix
3. **Step 3** — Analyze CI failure logs to confirm root cause
4. **Step 4** — Apply fix, then re-run the reproduce workflow to verify
5. **Step 5** — Clean up CI configuration

Each step has a **checkpoint** at the end. Do not proceed to the next step until the checkpoint is satisfied. Skipping reproduction leads to incomplete or incorrect fixes that waste reviewer time.

### Investigation Directory

Keep all investigation artifacts in a directory at the root of the repo:

```
flaky-test-investigation/
├── README.md              # Summary: test name, issue, root cause, fix, status
├── failure-logs/          # Downloaded CI failure logs
└── notes.md               # Running notes, observations, hypotheses
```

Create this directory at the start of your investigation. Commit it to the branch as you work — this allows:
- Post-investigation analysis of what went wrong
- Future agents to pick up interrupted work
- A record of the reproduce→fix→verify cycle

```bash
mkdir -p flaky-test-investigation/failure-logs
```

Initialize `README.md` with the test name, issue URL (if known), and current status. Update it as you progress through each step.

## Overview: The Reproduce→Fix→Verify Cycle

The steps below are sequential and gated. Complete each step fully before moving to the next.

1. Gather failure data from the issue (OS-specific failure rates, error messages)
2. Reproduce the failure on CI
3. Analyze failure logs to identify root cause
4. Apply a fix
5. Verify the fix by re-running the reproduce workflow
6. Clean up: reset CI configuration

**Always reproduce on CI first.** Do not attempt to fix the test before confirming the failure is reproducible.

## Step 1: Gather Failure Data

### Finding the Issue

The user may provide:
- A **test method name** (e.g., `DeployAsync_WithMultipleComputeEnvironments_Works`)
- A **GitHub issue URL** (e.g., `https://github.com/dotnet/aspire/issues/13287`)
- Both

**If you only have the test name**, find the tracking issue:

1. First check the test code for a `[QuarantinedTest]` attribute — it contains the issue URL:
   ```bash
   grep -rn "QuarantinedTest" tests/ --include="*.cs" | grep "TestMethodName"
   ```

2. If not found there, look up the test in the **quarantine tracking meta-issue** https://github.com/dotnet/aspire/issues/8813 — this issue tracks all quarantined tests with links to their individual issues:
   ```bash
   gh issue view 8813 --repo dotnet/aspire
   ```
   Search the output for the test name to find its linked issue.

3. If neither source has the issue, **proceed without historical failure data**. Use a default configuration (all 3 OSes, 5×5 iterations) since you don't know which OSes fail or the failure rate.

### From the Issue

Quarantined test issues contain tracking tables with per-OS failure rates over the last 100 runs. This data is critical:

- **Which OSes fail**: Target only those OSes to save runner time
- **Failure rate**: Determines how many iterations you need for reproduction
- **Error pattern**: Helps identify root cause before reproducing

```bash
# Read the issue to get failure data
gh issue view <issue-number> --repo dotnet/aspire
```

### From the Test Code

Find the test method, class, and project:

```bash
# Search for the test method
grep -rn "public.*async.*Task.*TestMethodName\|public.*void.*TestMethodName" tests/ --include="*.cs"
```

### Iteration Count Heuristic

Based on the failure rate from the issue tracking data:

| Failure Rate | Runners × Iterations per OS | Total per OS |
|---|---|---|
| >50% | 3 × 3 | 9 |
| 20-50% | 5 × 5 | 25 |
| 10-20% | 10 × 10 | 100 |
| <10% | 10 × 20 | 200 |

### ✅ Step 1 Checkpoint

Before proceeding to Step 2, confirm you have:
- [ ] The test method name, class, and project path
- [ ] The issue URL (if available)
- [ ] Per-OS failure rates (to choose target OSes and iteration counts)
- [ ] The error message/pattern from the issue

**Do NOT read the test source code to hypothesize a fix yet.** Proceed to Step 2 to reproduce first.

## Step 2: Reproduce on CI

### 2.1: Configure the Reproduce Workflow

Edit `.github/workflows/tests-reproduce.yml` — change only the `env:` section at the top:

```yaml
env:
  TEST_PROJECT: "Hosting.Azure"  # Project shortname
  TEST_FILTER: '--filter-method "*.DeployAsync_WithMultipleComputeEnvironments_Works"'
  TARGET_OSES: "ubuntu-latest,windows-latest"  # Only OSes that fail
  RUNNERS_PER_OS: "3"
  ITERATIONS_PER_RUNNER: "3"
```

**Test project shortname mapping**: The workflow resolves `TEST_PROJECT` to a path:
- Tries `tests/{name}.Tests/{name}.Tests.csproj` first
- Then `tests/Aspire.{name}.Tests/Aspire.{name}.Tests.csproj`
- Examples: `Hosting` → `Aspire.Hosting.Tests`, `Hosting.Azure` → `Aspire.Hosting.Azure.Tests`

**Common filter patterns**:
```yaml
# Single test method
TEST_FILTER: '--filter-method "*.TestMethodName"'
# All tests in a class
TEST_FILTER: '--filter-class "*.TestClassName"'
# Multiple test methods
TEST_FILTER: '--filter-method "*.Test1" --filter-method "*.Test2"'
```

**For quarantined tests**: The build step already includes `/p:RunQuarantinedTests=true`, so quarantined tests are automatically included. You do NOT need to add any special flags.

### 2.2: Enable the Reproduce Workflow in CI

In `.github/workflows/ci.yml`, temporarily swap the tests job to call `tests-reproduce.yml`:

```yaml
  # Comment out the normal tests job:
  # tests:
  #   uses: ./.github/workflows/tests.yml
  #   name: Tests
  #   needs: [prepare_for_ci]
  #   if: ${{ github.repository_owner == 'dotnet' && needs.prepare_for_ci.outputs.skip_workflow != 'true' }}
  #   with:
  #     versionOverrideArg: ${{ needs.prepare_for_ci.outputs.VERSION_SUFFIX_OVERRIDE }}

  # Temporarily use the reproduce workflow:
  tests:
    uses: ./.github/workflows/tests-reproduce.yml
    name: Tests
    needs: [prepare_for_ci]
    if: ${{ github.repository_owner == 'dotnet' && needs.prepare_for_ci.outputs.skip_workflow != 'true' }}
```

**Important**: You MUST revert ci.yml back to calling `tests.yml` before the PR is merged.

### 2.3: Push and Wait

```bash
git add .github/workflows/tests-reproduce.yml
git commit -m "Configure reproduce workflow for <test name>"
git push
```

Wait for the CI run (10-20 minutes). Monitor via:

```bash
# Watch the PR checks
gh pr checks <pr-number> --repo dotnet/aspire --watch

# Or find the run ID
gh run list --repo dotnet/aspire --branch <branch> --limit 1 --json databaseId,status
```

### 2.4: Analyze Results

**⛔ GATE: Do not proceed past this point until the CI run has completed and you have downloaded failure logs.** If no runners failed, increase iteration counts and re-run — do not assume the test is not flaky.

Each matrix job shows `<os> #<index>` (e.g., `ubuntu-latest #3`). Failed runners upload logs to `failures-<os>-<index>` artifacts.

```bash
# Download failure artifacts into the investigation directory
gh run download <run-id> --repo dotnet/aspire --dir flaky-test-investigation/failure-logs

# List what was downloaded
ls flaky-test-investigation/failure-logs/

# Commit the logs so they're preserved
git add flaky-test-investigation/
git commit -m "Add CI failure logs from run <run-id>"
```

**CRITICAL: Windows log encoding gotcha**

Windows CI log files are encoded as **UTF-16LE**. Running `cat` on them produces garbled output. Convert first:

```bash
# Convert Windows log to readable UTF-8
iconv -f UTF-16LE -t UTF-8 flaky-test-investigation/failure-logs/failures-windows-latest-1/test-output.log > flaky-test-investigation/failure-logs/readable-windows.log
cat flaky-test-investigation/failure-logs/readable-windows.log
```

**Alternatively**, search for the error directly:

```bash
# Search across all failure logs (handles encoding)
find flaky-test-investigation/failure-logs -name "*.log" -exec grep -l "Assert\|Error\|Exception" {} \;
```

## Step 3: Identify Root Cause

### Interpreting Reproduction Results

- **Some runners fail, some pass**: This is the expected pattern for a flaky test. Proceed to analyze the failures.
- **All runners fail (100%)**: Compare against the failure rate from the tracking issue. If the issue says e.g. 84% and you see 100%, that's consistent — proceed. But if the issue says e.g. 10% and you see 100%, this may be an **unrelated issue** (e.g., a build break, a new dependency problem). Investigate whether the failure is the same error as reported in the issue before attempting a fix.
- **No runners fail**: The test may not be reliably reproducible with your current iteration count. Increase `RUNNERS_PER_OS` and `ITERATIONS_PER_RUNNER` and try again.

### Analyzing Failure Logs

Look for the assertion or exception that failed:

```bash
# Find the actual test failure in logs
grep -A 10 "FAIL\|Assert\.\|Exception" flaky-test-investigation/failure-logs/readable-windows.log | head -50

# For .trx files (XML test results)
find flaky-test-investigation/failure-logs -name "*.trx" -exec grep -l 'outcome="Failed"' {} \;
```

Then find the corresponding test code and understand the concurrency/timing model.

### ✅ Step 3 Checkpoint

Before proceeding to Step 4, confirm you have:
- [ ] Downloaded and examined CI failure logs
- [ ] Identified the specific error (assertion failure, exception, timeout)
- [ ] Read the test source code and identified the root cause
- [ ] Documented the root cause in `flaky-test-investigation/README.md`

**Now — and only now — proceed to write the fix.**

## Step 4: Apply Fix and Verify

1. Make the code change
2. Keep `tests-reproduce.yml` configured for the same test
3. Commit and push:

```bash
git add -A
git commit -m "Fix flaky test: <description of fix>"
git push
```

4. Wait for CI to complete
5. If all iterations pass across all OSes, the fix is validated ✅

**If the fix doesn't work**: Iterate — read the new failure logs, refine the fix, push again.

## Step 5: Clean Up

Once the fix is verified:

### 5.1: DO NOT Unquarantine or Close the Issue

**Important policy**: A code fix alone is not sufficient to unquarantine a test. The test must have **zero failures across all OSes for 21 consecutive days** in the quarantine CI runs before it can be unquarantined. See `docs/unquarantine-policy.md`.

- **DO NOT** remove the `[QuarantinedTest]` attribute
- **DO NOT** close the tracking issue
- A separate process monitors the quarantine CI and handles unquarantining when the 21-day criteria are met

### 5.2: Reset the Reproduce Workflow

Reset `.github/workflows/tests-reproduce.yml` env vars to defaults:

```yaml
env:
  TEST_PROJECT: "Hosting"
  TEST_FILTER: '--filter-method "*.YourTestMethodName"'
  TARGET_OSES: "ubuntu-latest,windows-latest"
  RUNNERS_PER_OS: "3"
  ITERATIONS_PER_RUNNER: "3"
```

### 5.3: Revert CI Configuration

Revert `ci.yml` back to calling `tests.yml` — uncomment the original `tests:` job and remove the temporary `tests-reproduce.yml` call.

### 5.4: Final Commit

```bash
git add -A
git commit -m "Fix flaky test: <test name>

<brief description of fix>"
git push
```

**Note**: Keep the `flaky-test-investigation/` directory in the branch — it provides a record of the investigation. It will not be merged to `main` if the PR is squash-merged, but remains available on the branch for future reference.

## Key Technical Details

### Build System Quarantine Filtering

`eng/Testing.props` auto-appends `--filter-not-trait "quarantined=true"` to test arguments at **build time**. Even if you pass `--filter-trait quarantined=true` on the command line, the build already excluded them. The reproduce workflow handles this by passing `/p:RunQuarantinedTests=true` as an MSBuild property during build.

### test-reproduce.yml Architecture

The workflow:
1. **Setup job**: Parses env vars, generates a matrix of `{os, index}` combinations
2. **Reproduce jobs** (parallel): Each runner builds the test project once, then loops through iterations with DCP process cleanup between runs
3. **Results job**: Aggregates pass/fail across all runners into a summary table

Failed iterations upload their test output as artifacts named `failures-<os>-<index>`.

### workflow_dispatch Limitation

`workflow_dispatch` only works for workflows that exist on the default branch (`main`). Until `tests-reproduce.yml` is merged to `main`, you must trigger it through `ci.yml` (by temporarily editing it to call `tests-reproduce.yml`).

## Response Format

After completing a flaky test fix, provide a summary:

```markdown
## Flaky Test Fix Summary

### Test
- **Method**: `Namespace.Type.Method`
- **Issue**: #XXXXX
- **Project**: `tests/Aspire.{Project}.Tests/`

### Failure Data
| OS | Failure Rate |
|---|---|
| Windows | XX% |
| Linux | XX% |

### Root Cause
Brief description of what caused the flaky behavior.

### Fix
Description of the code change.

### Verification
| Run | Config | Result |
|-----|--------|--------|
| Pre-fix | X runners × Y iters × Z OSes | N failures ❌ |
| Post-fix | X runners × Y iters × Z OSes | All passed ✅ |

### Files Changed
- `path/to/file.cs` — description

### Next Steps
- Test remains quarantined — will be unquarantined after 21 days of zero failures
- Issue #XXXXX remains open — will be closed by the unquarantine process
```

## Important Constraints

- **Reproduce before fixing**: Always confirm the failure is reproducible before attempting a fix
- **Detect your OS**: Don't assume Linux — check with `uname -s` and decide if local reproduction is viable
- **Quarantined tests need /p:RunQuarantinedTests=true**: The build system filters them out by default
- **Keep investigation artifacts**: Save all logs, results, and notes in `flaky-test-investigation/` and commit them (but not crash dumps — `.gitignore` handles this)
- **DO NOT unquarantine or close issue**: The test stays quarantined until 21 days of zero failures (see `docs/unquarantine-policy.md`)
- **Minimize CI usage**: Use the iteration count heuristic to avoid wasting runners
- **Target specific OSes**: Only test on OSes that show failures in the tracking data
- **Build-verify everything**: After fixes, after any test attribute changes
- **Reset configuration**: Always reset tests-reproduce.yml and revert ci.yml when done
- **Don't fix unrelated issues**: If you encounter unrelated test failures, ignore them
- **Windows UTF-16LE**: Always handle encoding when reading Windows CI logs

## Appendix: Common Flaky Test Patterns

Reference this table when analyzing failure logs in Step 3. Do not use it to skip the reproduce step.

| Pattern                   | Symptom                                                                  | Fix                                                                               |
|---------------------------|--------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| Thread-unsafe collections | `Assert.Contains()` missing items; concurrent test fakes using `List<T>` | Replace `List<T>` with `ConcurrentBag<T>`                                         |
| Race condition on startup | Fails intermittently with timeout or "not started"                       | Use `WaitForHealthyAsync()` instead of `WaitForTextAsync("Application started.")` |
| Port conflicts            | `AddressInUseException`                                                  | Ensure `randomizePorts: true`                                                     |
| File locking (Windows)    | `IOException: The process cannot access the file`                        | Add retry logic or use temp directories                                           |
| Order-dependent state     | Passes alone, fails with other tests                                     | Ensure proper test isolation/cleanup                                              |
