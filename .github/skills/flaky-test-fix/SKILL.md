---
name: flaky-test-fix
description: Reproduces and fixes flaky or quarantined tests using the CI reproduce workflow (tests-reproduce.yml). Use this when asked to investigate, reproduce, debug, or fix a flaky test, a quarantined test, or an intermittently failing test.
---

You are a specialized agent for reproducing and fixing flaky tests in the dotnet/aspire repository. You use the CI reproduce workflow (`tests-reproduce.yml`) to reproduce failures across Windows/Linux/macOS.

## ⛔ MANDATORY: Follow the investigate→reproduce→fix→verify cycle

**Do NOT skip ahead to writing a code fix.** Even if you think you already know the root cause, you MUST follow every step in order:

1. **Step 1** — Gather failure data from the issue and read the test code for understanding
2. **Step 1.5** — Analyze existing quarantine failure logs (may reveal root cause without reproduction)
3. **Step 2** — Reproduce the failure on CI ← you MUST complete this before writing any fix (can be skipped — see Step 1.5)
4. **Step 3** — Analyze CI failure logs to confirm root cause
5. **Step 4** — Apply fix, then re-run the reproduce workflow to verify
6. **Step 5** — Clean up CI configuration

Each step has a **checkpoint** at the end. Do not proceed to the next step until the checkpoint is satisfied. Skipping reproduction leads to incomplete or incorrect fixes that waste reviewer time.

## Top-Level Tracking

Use SQL to track the overall investigation state. This keeps the main context clean and allows recovery if work is interrupted.

### Initialize tracking at the start of every investigation:

```sql
INSERT INTO todos (id, title, description, status) VALUES
  ('gather-data', 'Gather failure data', 'Read issue, find test code, determine failure rates per OS', 'pending'),
  ('analyze-existing', 'Analyze existing quarantine logs', 'Download logs from recent quarantine failures to understand the error', 'pending'),
  ('reproduce', 'Reproduce on CI', 'Configure and run tests-reproduce.yml to confirm the failure', 'pending'),
  ('analyze', 'Analyze failure logs', 'Download CI logs, identify root cause', 'pending'),
  ('fix', 'Apply fix', 'Write the code fix based on root cause analysis', 'pending'),
  ('verify', 'Verify fix on CI', 'Re-run reproduce workflow to confirm fix works', 'pending'),
  ('cleanup', 'Clean up CI config', 'Reset tests-reproduce.yml and ci.yml to defaults', 'pending');

INSERT INTO todo_deps (todo_id, depends_on) VALUES
  ('analyze-existing', 'gather-data'),
  ('reproduce', 'analyze-existing'),
  ('analyze', 'reproduce'),
  ('fix', 'analyze'),
  ('verify', 'fix'),
  ('cleanup', 'verify');
```

### Store key parameters in session state:

```sql
CREATE TABLE IF NOT EXISTS session_state (key TEXT PRIMARY KEY, value TEXT);
INSERT OR REPLACE INTO session_state (key, value) VALUES
  ('test_method', '<FullyQualifiedMethodName>'),
  ('test_project', '<ProjectShortname>'),
  ('issue_url', '<GitHubIssueURL>'),
  ('failure_rate_linux', '<rate or unknown>'),
  ('failure_rate_windows', '<rate or unknown>'),
  ('failure_rate_macos', '<rate or unknown>'),
  ('max_failure_rate', '<highest rate across OSes>'),
  ('reproduce_attempt', '1'),
  ('fix_attempt', '1'),
  ('reproduce_run_id', ''),
  ('verify_run_id', '');
```

**Always update todo status as you work** — set to `in_progress` before starting, `done` when complete. Query `SELECT * FROM todos;` to check progress. Store CI run IDs and attempt counts in `session_state`.

### Investigation Notes

Keep investigation notes in the **session workspace** (not in the repo). This avoids commit noise from temporary artifacts:

```
~/.copilot/session-state/<session-id>/
├── plan.md                # Summary: test name, issue, root cause, fix, status
└── files/
    └── failure-logs/      # Downloaded CI failure logs (if any)
```

Use `plan.md` in the session workspace for running notes and observations. Only create files in the repo if the investigation needs to be resumed by another agent in a different session.

## Overview: The Investigate→Reproduce→Fix→Verify Cycle

The steps below are sequential and gated. Complete each step fully before moving to the next.

1. Gather failure data from the issue (OS-specific failure rates, error messages) and read the test code for understanding
2. Analyze existing quarantine failure logs — this often reveals the root cause without needing a separate reproduction
3. Reproduce the failure on CI (may require scaling up iterations) — can be skipped if existing logs clearly reveal the root cause AND the test is identified as contention-sensitive (see Step 1.5)
4. Analyze failure logs to identify root cause
5. Apply a fix
6. Verify the fix by re-running the reproduce workflow (scaled to original failure rate)
7. Clean up: reset CI configuration

**Prefer analyzing existing data first.** The quarantine CI runs every 6 hours and the tracking issue links to runs with failures. These logs are often sufficient to diagnose the root cause without a separate reproduction run.

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

Find the test method, class, and project. **Read the test source code and its fixture/setup** to understand what the test does, how it waits for readiness, and what patterns it uses. This is essential for understanding what you're trying to reproduce and for matching against the common flaky test patterns table (see Appendix).

```bash
# Search for the test method
grep -rn "public.*async.*Task.*TestMethodName\|public.*void.*TestMethodName" tests/ --include="*.cs"
```

**Consult the Common Flaky Test Patterns table** (Appendix) early. If the test code matches a known pattern AND the error message from the issue matches the expected symptom, you have a strong hypothesis to validate during reproduction.

### Iteration Count Heuristic

Based on the failure rate from the issue tracking data, calculate iterations to achieve **95% probability of seeing at least one failure** (if the bug exists):

| Failure Rate | Runners × Iterations per OS | Total per OS | Confidence |
|---|---|---|---|
| >50% | 3 × 3 | 9 | >99% |
| 20-50% | 5 × 5 | 25 | >99% |
| 10-20% | 5 × 10 | 50 | >99% |
| 5-10% | 10 × 10 | 100 | >99% |
| <5% | 10 × 25 | 250 | >95% |

The math: for failure rate `p`, need `n ≥ log(0.05) / log(1-p)` iterations for 95% confidence. The table above provides comfortable margins.

### ✅ Step 1 Checkpoint

Before proceeding to Step 1.5, confirm you have:
- [ ] The test method name, class, and project path
- [ ] The issue URL (if available)
- [ ] Per-OS failure rates (to choose target OSes and iteration counts)
- [ ] The error message/pattern from the issue
- [ ] Read the test source code and its fixture/setup for understanding
- [ ] Checked the Common Flaky Test Patterns table for matches
- [ ] SQL tracking initialized with all parameters stored

**Do NOT write a fix yet.** You have a hypothesis, but proceed to Step 1.5 to validate it with existing failure data.

## Step 1.5: Analyze Existing Quarantine Failure Logs

Before running a separate reproduction, check if existing quarantine CI logs already contain the information you need. The quarantine workflow runs every 6 hours, and the tracking issue links to recent failures.

### Finding Failure Logs from Quarantine Runs

The tracking issue contains ❌ links to failed quarantine runs. Use those run IDs to find the specific job that failed:

```bash
# Find the failed job for your test project in a quarantine run
gh api "repos/dotnet/aspire/actions/runs/<run_id>/jobs?per_page=100&filter=latest" \
  --jq '.jobs[] | select(.name | contains("<ProjectShortname>")) | select(.conclusion == "failure") | {id: .id, name: .name}'
```

Then download the logs for that job:

```bash
# Get logs via the GitHub MCP tool (preferred — handles encoding automatically)
# Use get_job_logs with the job_id, return_content: true, tail_lines: 300

# Or via CLI
gh api "repos/dotnet/aspire/actions/jobs/<job_id>/logs" > quarantine-failure.log
```

Search the logs for the test name, error message, and stack trace:

```bash
grep -i "TestMethodName\|TaskCanceled\|Assert\|Exception\|FAIL" quarantine-failure.log | head -30
```

### Identifying Contention-Sensitive Tests

A test is likely **contention-sensitive** (fails only when running alongside other tests) if:

1. **It uses `randomizePorts: false`** — fixed ports can conflict with other concurrent tests
2. **It uses a shared fixture** (collection fixture or class fixture) — startup timing depends on other tests
3. **It uses `WaitForTextAsync`** — log-based readiness checks are fragile under contention
4. **It shares a `CancellationTokenSource` across startup and readiness phases** — one phase can starve the other's timeout budget
5. **The tracking issue shows 0% failure on macOS** (which often has less CI contention) but failures on Linux/Windows

If you identify the test as contention-sensitive, the reproduce workflow (which runs the test in isolation) is unlikely to reproduce the failure. In this case, you may **skip Step 2** and proceed directly to Step 3 (root cause analysis) using the quarantine logs as your evidence.

### ✅ Step 1.5 Checkpoint

Before deciding whether to skip reproduction:
- [ ] Downloaded and examined at least 1-2 quarantine failure logs for the test
- [ ] Confirmed the error matches the pattern in the tracking issue
- [ ] Assessed whether the test is contention-sensitive

**If contention-sensitive**: Mark `reproduce` as `done` in SQL, set `analyze` to `in_progress`, and proceed to Step 3 using the quarantine logs as your failure evidence.

**If NOT contention-sensitive** (or you're unsure): Proceed to Step 2 for reproduction.

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

### 2.3: Push and Monitor

```bash
git add .github/workflows/tests-reproduce.yml .github/workflows/ci.yml
git commit -m "Configure reproduce workflow for <test name>"
git push
```

**Monitor the run using polling** (CI runs take 10-30+ minutes):

```bash
# Find the run ID
gh run list --repo dotnet/aspire --branch <branch> --limit 1 --json databaseId,status
```

Store the run ID, then poll periodically for completion:
```sql
INSERT OR REPLACE INTO session_state (key, value) VALUES ('reproduce_run_id', '<run-id>');
```

```bash
# Poll for completion (use bash mode="async", then read_bash with increasing delays)
# Avoid `gh run watch` — it produces excessive output that floods the context window.
gh run view <run-id> --repo dotnet/aspire --json status,conclusion --jq '{status, conclusion}'

# Check individual job results as they complete
gh run view <run-id> --repo dotnet/aspire --json jobs \
  --jq '.jobs[] | select(.status == "completed") | {name: .name, conclusion: .conclusion}'
```

**Tip**: Use `gh run watch` with bash `mode="async"` only as a background blocker. Don't read its output — instead use the targeted `gh run view` queries above to check progress.

### 2.4: Handle Reproduction Results

**⛔ GATE: Do not proceed past this point until the CI run has completed.**

If there are failure artifacts, download them:

```bash
# Download failure artifacts
gh run download <run-id> --repo dotnet/aspire --dir /tmp/failure-logs

# Or get logs directly via the GitHub API / MCP tools
gh api "repos/dotnet/aspire/actions/jobs/<job_id>/logs" > /tmp/failure.log
```

**Distinguishing test failures from infrastructure failures:**

CI runners sometimes fail due to infrastructure issues, NOT the test itself. Common infrastructure failures include:
- `Failed to install or invoke dotnet...` (exit code -1073741502 on Windows)
- `The runner has received a shutdown signal` or runner timeouts
- Network connectivity errors during `dotnet restore`

**These do NOT count as reproductions.** Check the actual error message — only count iterations where the **test itself** failed with the expected error pattern from the tracking issue.

**If some runners show test failures (the expected error)**: Reproduction successful ✅. Proceed to Step 3.

**If no runners show the expected test failure — scale up and retry:**

```sql
-- Track the scaling attempt
INSERT OR REPLACE INTO session_state (key, value)
VALUES ('reproduce_attempt', CAST((SELECT CAST(value AS INTEGER) FROM session_state WHERE key = 'reproduce_attempt') + 1 AS TEXT));
```

Scale up progressively, focusing on the OS most likely to fail first (based on per-OS failure rates from the issue). Go back to Step 2.1 after each change:

| Attempt | `TARGET_OSES` | `RUNNERS_PER_OS` | `ITERATIONS_PER_RUNNER` | Notes |
|---------|---------------|-------------------|--------------------------|-------|
| 1 | Highest-failure-rate OS only | From heuristic table | From heuristic table | Start narrow — one OS, sized by failure rate |
| 2 | Same single OS | Same | 2× previous | Double `ITERATIONS_PER_RUNNER` only |
| 3 | Add second-worst OS (if available) | Same | Same as attempt 2 | Expand OS coverage, keep iteration count |

**Upper bounds**: Do not exceed `RUNNERS_PER_OS=10` or `ITERATIONS_PER_RUNNER=50` (total matrix entries must stay ≤ 256 per GitHub Actions limits).

**If 2+ attempts at ≥95% confidence produce zero test failures**: The test is likely **contention-sensitive** — it only fails when running alongside other tests, which the reproduce workflow doesn't simulate. In this case:
1. Fall back to analyzing existing quarantine failure logs (Step 1.5)
2. Read the test code to identify contention indicators (shared ports, shared fixtures, sequential waits)
3. Proceed to Step 3 using quarantine logs as your failure evidence
4. The verification run (Step 4) will still validate your fix in isolation, which is useful even if you can't reproduce the original failure

**CRITICAL: Windows log encoding gotcha**

Windows CI log files downloaded as artifacts are encoded as **UTF-16LE**. Running `cat` on them produces garbled output. Convert first:

```bash
# Convert Windows log to readable UTF-8
iconv -f UTF-16LE -t UTF-8 /tmp/failure-logs/failures-windows-latest-1/test-output.log > /tmp/readable-windows.log
cat /tmp/readable-windows.log
```

**Tip**: Using `get_job_logs` via GitHub API/MCP tools returns UTF-8 directly, avoiding encoding issues entirely. Prefer API-based log retrieval when possible.

**Alternatively**, search for the error directly:

```bash
# Search across all failure logs (handles encoding)
find /tmp/failure-logs -name "*.log" -exec grep -l "Assert\|Error\|Exception" {} \;
```

## Step 3: Identify Root Cause

### Interpreting Reproduction Results

- **Some runners fail, some pass**: This is the expected pattern for a flaky test. Proceed to analyze the failures.
- **All runners fail (100%)**: Compare against the failure rate from the tracking issue. If the issue says e.g. 84% and you see 100%, that's consistent — proceed. But if the issue says e.g. 10% and you see 100%, this may be an **unrelated issue** (e.g., a build break, a new dependency problem). Investigate whether the failure is the same error as reported in the issue before attempting a fix.
- **No runners fail**: The test may not be reliably reproducible with your current iteration count. Increase `RUNNERS_PER_OS` and `ITERATIONS_PER_RUNNER` and try again.

### Analyzing Failure Logs

Failure logs may come from reproduce runs (Step 2) or existing quarantine runs (Step 1.5). Both are valid sources.

**Preferred: Use GitHub API/MCP tools** to get logs directly (avoids encoding issues):

```bash
# Get job logs via GitHub MCP tool: get_job_logs with job_id, return_content: true, tail_lines: 300
# Or via CLI:
gh api "repos/dotnet/aspire/actions/jobs/<job_id>/logs" > /tmp/failure.log
```

**Delegate log analysis to a sub-agent** to keep the main context clean:

```
Use a task agent (explore or general-purpose) to analyze the failure logs:
- Pass the log file paths or content
- Ask it to identify the specific assertion/exception
- Ask it to read the test source code and identify the concurrency/timing model
- Have it return a structured root cause summary
```

Look for the assertion or exception that failed:

```bash
# Find the actual test failure in logs
grep -A 10 "FAIL\|Assert\.\|Exception" /tmp/failure.log | head -50

# For .trx files (XML test results) from downloaded artifacts
find /tmp/failure-logs -name "*.trx" -exec grep -l 'outcome="Failed"' {} \;
```

Then find the corresponding test code and understand the concurrency/timing model.

### ✅ Step 3 Checkpoint

Before proceeding to Step 4, confirm you have:
- [ ] Examined CI failure logs (from reproduce runs OR existing quarantine runs)
- [ ] Identified the specific error (assertion failure, exception, timeout)
- [ ] Read the test source code and identified the root cause
- [ ] Documented the root cause in your session plan

**Now — and only now — proceed to write the fix.**

## Step 4: Apply Fix and Verify

### 4.1: Apply the Fix

1. Make the code change
2. **Build locally to confirm it compiles**:
   ```bash
   dotnet build tests/<TestProject>.Tests/<TestProject>.Tests.csproj --no-restore -v:q
   ```
3. Keep `tests-reproduce.yml` configured for the same test

### 4.2: Choose Verification Scale

The verification run must be large enough to be confident the fix works. Use the **original failure rate** to determine scale — you need enough iterations that, if the bug were still present, it would have manifested with ≥95% probability.

**Verification iteration heuristic** (same math as reproduction — `n ≥ log(0.05) / log(1-p)`):

| Original Failure Rate | Runners × Iterations per OS | Total per OS | 95% Detection Confidence |
|---|---|---|---|
| >50% | 3 × 3 | 9 | ✅ Would catch >99.8% of the time |
| 20-50% | 5 × 5 | 25 | ✅ Would catch >99% of the time |
| 10-20% | 5 × 10 | 50 | ✅ Would catch >99% of the time |
| 5-10% | 10 × 10 | 100 | ✅ Would catch >95% of the time |
| <5% | 10 × 25 | 250 | ✅ Would catch >95% of the time |

For tests with very low failure rates (<5%), consider whether the verification is practical within CI budget constraints. If not, document the limitation and rely on the 21-day quarantine monitoring to confirm.

**For contention-sensitive tests** (where reproduction in isolation didn't work): The verification run still validates that the fix doesn't break the test. Use the failure rate heuristic table above to size the verification — even though the reproduce workflow runs in isolation, a passing verification provides baseline confidence. The 21-day quarantine monitoring will provide the definitive confirmation under real contention.

### 4.3: Push and Verify

```bash
git add -A
git commit -m "Fix flaky test: <description of fix>"
git push
```

Store the verification run ID:
```sql
INSERT OR REPLACE INTO session_state (key, value) VALUES ('verify_run_id', '<run-id>');
INSERT OR REPLACE INTO session_state (key, value) VALUES ('fix_attempt', '1');
```

Wait for CI to complete. Monitor with polling (`gh run view --json status,conclusion`), not `gh run watch`.

### 4.4: Handle Verification Results

**If all iterations pass across all OSes**: The fix is validated ✅. Proceed to Step 5.

**If some iterations still fail**: The fix is incomplete or incorrect. Iterate:

```sql
-- Track the fix attempt
INSERT OR REPLACE INTO session_state (key, value)
VALUES ('fix_attempt', CAST((SELECT CAST(value AS INTEGER) FROM session_state WHERE key = 'fix_attempt') + 1 AS TEXT));
```

1. Download the new failure logs:
   ```bash
   gh run download <run-id> --repo dotnet/aspire --dir /tmp/failure-logs
   ```
2. Analyze the new failure pattern — is it the same error or a different one?
3. Refine the fix based on the new evidence
4. Push and re-verify

**After 3 failed fix attempts**: Stop and report findings to the user. The issue may require deeper architectural changes or domain expertise.

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

<brief description of fix>

Fixes #<issue-number>"
git push
```

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

- **Reproduce before fixing**: Always confirm the failure is reproducible before attempting a fix — but for contention-sensitive tests, existing quarantine logs may serve as sufficient evidence (see Step 1.5)
- **Detect your OS**: Don't assume Linux — check with `uname -s` and decide if local reproduction is viable
- **Quarantined tests need /p:RunQuarantinedTests=true**: The build system filters them out by default
- **Keep investigation notes in session workspace**: Use `plan.md` and `files/` in the session workspace, not a directory in the repo
- **Distinguish infrastructure vs test failures**: CI runners sometimes fail due to infrastructure issues (e.g., `Failed to install or invoke dotnet...` on Windows). These do NOT count as test reproductions. Always verify the error matches the expected test failure pattern.
- **DO NOT unquarantine or close issue**: The test stays quarantined until 21 days of zero failures (see `docs/unquarantine-policy.md`)
- **Scale verification to failure rate**: A 50% failure rate test needs fewer verification iterations than a 5% failure rate test. Use the verification heuristic table.
- **Target specific OSes**: Only test on OSes that show failures in the tracking data
- **Build-verify everything**: After fixes, after any test attribute changes
- **Reset configuration**: Always reset tests-reproduce.yml and revert ci.yml when done
- **Don't fix unrelated issues**: If you encounter unrelated test failures, ignore them
- **Windows UTF-16LE**: Always handle encoding when reading Windows CI logs downloaded as files (not needed when using `get_job_logs` via GitHub API/MCP, which returns UTF-8)
- **Prefer polling over `gh run watch`**: Use `gh run view --json status,conclusion` to check CI status — `gh run watch` produces excessive output that floods the context window
- **Use sub-agents for heavy work**: Delegate log analysis and CI monitoring to sub-agents to keep main context clean
- **Track state in SQL**: Use the todos table and session_state for tracking progress across the investigate→reproduce→fix→verify cycle

## Appendix: Common Flaky Test Patterns

Consult this table during Step 1 (gather data) to form hypotheses, and during Step 3 (analysis) to confirm root causes.

| Pattern                   | Symptom                                                                  | Fix                                                                               |
|---------------------------|--------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| Thread-unsafe collections | `Assert.Contains()` missing items; concurrent test fakes using `List<T>` | Replace `List<T>` with `ConcurrentBag<T>`                                         |
| Race condition on startup | Fails intermittently with timeout or "not started"                       | Use `WaitForHealthyAsync()` instead of `WaitForTextAsync("Application started.")` |
| Shared timeout budget     | `TaskCanceledException` in fixture `InitializeAsync`; one phase starves the other | Use separate `CancellationTokenSource` for each phase (startup vs readiness)      |
| Sequential service waits  | `TaskCanceledException` in `WaitReadyStateAsync`; timeout under CI load  | Wait for services in parallel with `Task.WhenAll` instead of sequentially         |
| Port conflicts            | `AddressInUseException`                                                  | Ensure `randomizePorts: true`                                                     |
| File locking (Windows)    | `IOException: The process cannot access the file`                        | Add retry logic or use temp directories                                           |
| Order-dependent state     | Passes alone, fails with other tests                                     | Ensure proper test isolation/cleanup                                              |
| Contention-only failure   | Passes 100% in isolation, fails 10-20% in quarantine runs               | Look for shared resources (ports, CTS, fixtures); parallelize waits; add margins  |
