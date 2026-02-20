# Reproducing and Fixing Flaky Tests

This document describes how to reproduce and fix flaky tests in the Aspire repository, using both local scripts and CI workflows.

## Overview

Flaky tests fail intermittently. Some can be reproduced locally, but others only fail on specific OSes or under CI conditions. We have two tools:

| Tool | When to use |
|------|-------------|
| `run-test-100-times.sh` | Local reproduction on your current OS |
| `tests-reproduce.yml` | CI reproduction across Windows/macOS/Linux |

## Local Reproduction: `run-test-100-times.sh`

Use this first if the flaky test fails on your local OS.

```bash
# Run a specific test 100 times, stop on first failure
./run-test-100-times.sh -- dotnet test tests/Aspire.Hosting.Tests/Aspire.Hosting.Tests.csproj \
  --no-build -- --filter-method "*.TestProjectStartsAndStopsCleanly"

# Run 50 times, don't stop on failure
./run-test-100-times.sh -n 50 --run-all -- dotnet test tests/Aspire.Hosting.Tests/Aspire.Hosting.Tests.csproj \
  --no-build -- --filter-method "*.TestProjectStartsAndStopsCleanly"
```

Results are saved to `/tmp/test-results-<timestamp>/`. If the test passes all iterations locally, move on to CI reproduction.

## CI Reproduction: `tests-reproduce.yml`

### How It Works

The workflow fans out to multiple parallel runners across selected OSes. Each runner builds the test project once, then runs the test multiple times with cleanup between iterations.

**Total test executions** = `RUNNERS_PER_OS` × `ITERATIONS_PER_RUNNER` × number of OSes

For example: 5 runners × 3 iterations × 3 OSes = 45 test executions.

### How to Use

#### Step 1: Edit the Configuration

Open `.github/workflows/tests-reproduce.yml` and edit the `env:` section at the top:

```yaml
env:
  # Test project shortname (e.g., Hosting, Core, Dashboard)
  TEST_PROJECT: "Hosting"

  # Test filter (passed after -- to dotnet test)
  TEST_FILTER: '--filter-method "*.TestProjectStartsAndStopsCleanly"'

  # Target OSes (comma-separated)
  TARGET_OSES: "ubuntu-latest,windows-latest,macos-latest"

  # Parallel runners per OS
  RUNNERS_PER_OS: "5"

  # Iterations per runner
  ITERATIONS_PER_RUNNER: "3"
```

**Common filter patterns:**

```yaml
# Single test method
TEST_FILTER: '--filter-method "*.TestProjectStartsAndStopsCleanly"'

# All tests in a class
TEST_FILTER: '--filter-class "*.SlimTestProgramTests"'

# Multiple test methods
TEST_FILTER: '--filter-method "*.Test1" --filter-method "*.Test2"'
```

**For quarantined tests**, the build step already passes `/p:RunQuarantinedTests=true` so quarantined tests are included.

#### Step 2: Wire into CI (if not already)

In `.github/workflows/ci.yml`, the `tests:` job should call `tests-reproduce.yml`:

```yaml
tests:
  uses: ./.github/workflows/tests-reproduce.yml
  name: Tests
  needs: [prepare_for_ci]
  if: ...
```

#### Step 3: Commit and Push

```bash
git add .github/workflows/tests-reproduce.yml
git commit -m "Configure reproduce workflow for <test name>"
git push
```

#### Step 4: Monitor Results

- Check the PR's Actions tab for the workflow run
- Each matrix job shows `<os> #<index>` (e.g., `ubuntu-latest #3`)
- Failed runners upload logs to `failures-<os>-<index>` artifacts
- The results job shows a summary table

#### Step 5: Iterate

1. Download failure artifacts and analyze logs
2. Make your fix
3. Edit `tests-reproduce.yml` if needed (e.g., change filters, add OSes)
4. Commit and push — CI runs again
5. Repeat until all iterations pass

### Reverting CI to Normal

When done, revert `ci.yml` to call `tests.yml`:

```yaml
tests:
  uses: ./.github/workflows/tests.yml
  name: Tests
  needs: [prepare_for_ci]
  if: ...
  with:
    versionOverrideArg: ${{ needs.prepare_for_ci.outputs.VERSION_SUFFIX_OVERRIDE }}
```

### Future: Direct Workflow Dispatch

Once `tests-reproduce.yml` exists on `main`, trigger it directly without modifying ci.yml:

```bash
gh workflow run tests-reproduce.yml \
  --ref my-branch \
  -f testProject=Hosting \
  -f testFilter='--filter-method "*.MyFlakyTest"'
```

## Test Project Shortnames

The `TEST_PROJECT` maps to test project paths:

| Shortname | Project Path |
|-----------|-------------|
| `Hosting` | `tests/Aspire.Hosting.Tests/` |
| `Core` | `tests/Aspire.Core.Tests/` (or similar) |
| `Dashboard` | `tests/Aspire.Dashboard.Tests/` |

The workflow tries `tests/{name}.Tests/` first, then `tests/Aspire.{name}.Tests/`.

---

## Instructions for Copilot Agents

### Fixing a Flaky Test

When asked to fix a flaky test:

1. **Identify the test**: Find the test method, class, and project from the issue or quarantine attribute.

2. **Try local reproduction first** (if the OS matches):
   ```bash
   # Build the project first
   ./build.sh -restore -build -projects tests/Aspire.{Project}.Tests/Aspire.{Project}.Tests.csproj

   # Run repeatedly
   ./run-test-100-times.sh -n 20 -- dotnet test tests/Aspire.{Project}.Tests/Aspire.{Project}.Tests.csproj \
     --no-build -- --filter-method "*.{TestMethodName}"
   ```

3. **If local reproduction fails or wrong OS**, use CI reproduction:
   - Edit `.github/workflows/tests-reproduce.yml`:
     - Set `TEST_PROJECT` to the project shortname
     - Set `TEST_FILTER` to target the specific test(s)
     - Set `TARGET_OSES` to the relevant OS(es)
     - For quarantined tests, the build already includes `/p:RunQuarantinedTests=true`
   - Ensure `.github/workflows/ci.yml` calls `tests-reproduce.yml` (see above)
   - Commit and push the changes
   - Wait for the PR's CI workflow to complete
   - Check job results: look for `❌ Iteration N: FAIL` in job logs
   - Download failure artifacts for detailed logs

4. **Analyze the failure**:
   - Look at test output in the iteration logs
   - Check for race conditions, timing issues, or OS-specific behavior
   - Common patterns:
     - Waiting for log messages instead of health checks → use `WaitForHealthyAsync`
     - Port conflicts → ensure `randomizePorts: true`
     - Docker-dependent tests on non-Linux → skip appropriately

5. **Apply the fix and verify**:
   - Make the code change
   - Keep `tests-reproduce.yml` configured for the same test
   - Commit and push — CI will re-run the reproduce workflow
   - If all iterations pass across all OSes, the fix is validated

6. **Clean up**:
   - Revert `ci.yml` to call `tests.yml` instead of `tests-reproduce.yml`
   - Reset `tests-reproduce.yml` configuration to defaults
   - Remove the `[QuarantinedTest]` attribute from the fixed test (use QuarantineTools)
   - Commit the final changes
