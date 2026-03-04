---
name: fix-flaky-test
description: Reproduces and fixes flaky or quarantined tests. Tries local reproduction first (fast), then falls back to CI reproduce workflow (reproduce-flaky-tests.yml). Use this when asked to investigate, reproduce, debug, or fix a flaky test, a quarantined test, or an intermittently failing test.
---

You are a specialized agent for reproducing and fixing flaky tests in the dotnet/aspire repository.

## ⛔ Hard Rules

1. **DO NOT** use `Fixes #`, `Closes #`, or `Resolves #` in any PR body — this auto-closes the tracking issue.
2. **DO NOT** remove the `[QuarantinedTest]` attribute — unquarantining is a separate process after 21 days of zero failures.
3. **DO NOT** open the final PR until CI verification has completed and you have validated the results.
4. **DO NOT** skip reproduction — even if you think you know the root cause, follow every phase in order.
5. **DO NOT** close the tracking issue — that happens via the unquarantine process, not your PR.

## Process Overview

Follow these phases sequentially. Each phase has a goal and a done-when gate.

| # | Phase | Goal |
|---|-------|------|
| 1 | Gather data | Understand the test, its failure rates, and form a hypothesis |
| 2 | Reproduce locally | Fast-path validation (~minutes) using `run-test-repeatedly` |
| 3 | Reproduce on CI | Cross-OS, parallel-runner reproduction via `reproduce-flaky-tests.yml` |
| 4 | Analyze & fix | Identify root cause from logs, apply code fix |
| 5 | Verify | Prove the fix works locally then on CI |
| 6 | Clean up & PR | Squash to clean commit, open final PR, validate it |

## Phase 1: Gather Data

**Goal:** Understand the test, which OSes fail, at what rate, and form a root cause hypothesis.

**Steps:**

1. **Find the issue.** The user provides a test name, issue URL, or both.
   - If you only have the test name, check for a `[QuarantinedTest]` attribute (contains the issue URL), or look up the test in the meta-issue https://github.com/dotnet/aspire/issues/8813.
   - If no issue exists, proceed with default config (all OSes, 3×10 iterations).

2. **Read the issue** to get per-OS failure rates, error messages, and linked quarantine run failures.

3. **Read the test source code** and its fixture/setup. Understand what it does, how it waits for readiness, and what concurrency model it uses.

4. **Check for known flaky patterns** in `.github/instructions/test-review-guidelines.instructions.md`. If the code matches a pattern AND the error matches, you have a strong hypothesis.

5. **Analyze existing quarantine failure logs** — the quarantine CI runs every 6 hours. Download logs from recent failures linked in the issue:
   ```bash
   gh api "repos/dotnet/aspire/actions/runs/<run_id>/jobs?per_page=100&filter=latest" \
     --jq '.jobs[] | select(.name | contains("<ProjectShortname>")) | select(.conclusion == "failure") | {id: .id, name: .name}'
   gh api "repos/dotnet/aspire/actions/jobs/<job_id>/logs" > quarantine-failure.log
   ```

**Done when:** You have the test name, project, issue URL, per-OS failure rates, and have read the test code.

## Phase 2: Reproduce Locally

**Goal:** Fast-path reproduction on your local machine (~minutes vs ~30min for CI).

**Viable when:** Your OS matches a failing OS, or the test fails on all OSes.

```bash
# Check your OS
uname -s  # Darwin = macOS, Linux = Linux

# Build the test project (use /p:RunQuarantinedTests=true for quarantined tests)
dotnet build tests/<TestProject>.Tests/<TestProject>.Tests.csproj -v:q /p:RunQuarantinedTests=true

# Run repeatedly
./.github/skills/fix-flaky-test/run-test-repeatedly.sh -n 20 -- \
  dotnet test tests/<TestProject>.Tests/<TestProject>.Tests.csproj --no-build \
  /p:RunQuarantinedTests=true \
  -- --filter-method "*.<TestMethodName>"
```

Choose iteration count based on failure rate. If unsure, consult REFERENCE.md § Verification Scale.

**Done when:** Either you reproduced the failure locally (proceed to Phase 4), or local reproduction failed and you proceed to Phase 3.

## Phase 3: Reproduce on CI

**Goal:** Prove the test fails without your fix using the reproduce workflow across CI runners.

### 3.1: Create investigation branch and disable ci.yml

```bash
git checkout -b <branch-name>
```

Disable `ci.yml` so pushes don't trigger full CI — change the `on:` trigger:
```yaml
on:
  workflow_dispatch: {}  # Only manual trigger
```

### 3.2: Configure the reproduce workflow

**Tool:** `configure-reproduce.ps1`

```pwsh
.github/skills/fix-flaky-test/configure-reproduce.ps1 `
  -Project "Hosting.Azure" `
  -Method "TestMethodName" `
  -OS "windows-latest" `
  -Commit
```

For OS targeting strategy, consult REFERENCE.md § OS Targeting.

### 3.3: Push and trigger

**⛔ This commit must contain ONLY workflow changes — no code fixes.** The purpose is to establish a baseline proving the test fails without your fix.

```bash
git push --set-upstream origin <branch-name>
gh workflow run reproduce-flaky-tests.yml --repo dotnet/aspire --ref <branch-name>
```

If workflow dispatch fails with HTTP 403, your token lacks `actions:write`. Document this in the PR — continue with the investigation.

### 3.4: Monitor and validate results

Poll for completion (avoid `gh run watch` — it floods context):
```bash
gh run list --repo dotnet/aspire --branch <branch> --limit 1 --json databaseId,status
gh run view <run-id> --repo dotnet/aspire --json status,conclusion --jq '{status, conclusion}'
```

**⛔ Validate EVERY job before interpreting results:**
```bash
gh run view <run-id> --repo dotnet/aspire --json jobs \
  --jq '.jobs[] | select(.name != "Generate matrix" and .name != "Reproduce Results") | {name: .name, conclusion: .conclusion}'
```

Categorize each job:

| Category | Counts as |
|----------|-----------|
| ✅ Test passed (logs show `Passed:` with count ≥ 1) | Valid pass |
| ❌ Test failed (expected error from issue) | Valid reproduction |
| ⚠️ Zero tests ran (`Total: 0` or "Zero tests executed") | **Invalid — not a pass or failure** |
| 🔧 Infrastructure failure (runner/SDK/network errors) | **Invalid — does not count** |

**⛔ Zero-test runs are NOT passes.** If a Docker-dependent test runs on Windows, all Windows jobs will show zero tests. Exclude those OSes from counts and state this explicitly.

### 3.5: Handle failure to reproduce

If no test failures after initial attempt, scale up: double `ITERATIONS_PER_RUNNER`, re-trigger. Cancel old runs first:

```bash
gh run cancel <old-run-id> --repo dotnet/aspire
```

After 2 failed single-test attempts → escalate to **quarantine-project mode**: manually edit the `TEST_FILTER` in the workflow YAML to `--filter-trait "quarantined=true"` with `RUNNERS_PER_OS: "3"` and `ITERATIONS_PER_RUNNER: "3"`. This recreates the contention from the quarantine CI.

If quarantine-project mode also fails → fall back to log-based analysis using existing quarantine failure logs. Note this in the PR.

**Done when:** You have CI evidence showing the test fails, or you've documented why reproduction wasn't possible and are proceeding with log-based analysis.

## Phase 4: Analyze & Fix

**Goal:** Identify root cause from failure logs, apply the code fix.

1. **Download failure logs** from CI or use existing quarantine logs:
   ```bash
   gh run download <run-id> --repo dotnet/aspire --dir /tmp/failure-logs
   ```
   For Windows logs, consult REFERENCE.md § Windows Encoding.

2. **Delegate log analysis** to a sub-agent to keep main context clean — pass log file paths and ask it to identify the specific assertion/exception and match against the test's concurrency model.

3. **Apply the fix.** Keep it minimal — only change what's needed.

4. **Build locally** to confirm compilation:
   ```bash
   dotnet build tests/<TestProject>.Tests/<TestProject>.Tests.csproj --no-restore -v:q
   ```

**Done when:** You have a root cause, a fix that compiles, and can explain both in 1-2 sentences.

## Phase 5: Verify

**Goal:** Prove the fix eliminates the failure — locally first, then always on CI.

### 5.1: Local verification (fast pre-check)

If local reproduction succeeded in Phase 2, run the same test with your fix:

```bash
dotnet build tests/<TestProject>.Tests/<TestProject>.Tests.csproj --no-restore -v:q
./.github/skills/fix-flaky-test/run-test-repeatedly.sh -n 20 -- \
  dotnet test tests/<TestProject>.Tests/<TestProject>.Tests.csproj --no-build \
  -- --filter-method "*.<TestMethodName>"
```

If it fails, iterate on the fix before going to CI.

### 5.2: CI verification

Push the fix as a **separate commit** (no workflow changes in this commit):

```bash
git add -A
git diff --cached --stat  # Verify: no .github/workflows/ files
git commit -m "Fix flaky test: <description>"
git push
gh workflow run reproduce-flaky-tests.yml --repo dotnet/aspire --ref <branch-name>
```

For CI scale, use the defaults (3×10) for high confidence, or consult REFERENCE.md § Verification Scale for scaling guidance.

If reproduction used quarantine-project mode, use the same mode for verification.

### 5.3: Validate verification results

Apply the same job validation rules from Phase 3.4. The verification run should show significantly better results than the reproduction run.

**If all test-executing iterations pass:** Verification successful ✅ — proceed to Phase 6.

**If some still fail:** Iterate on the fix. After 3 failed attempts, stop and report to the user.

**Done when:** CI verification completed and all test-executing iterations pass.

## Phase 6: Clean Up & PR

**Goal:** Create a clean PR with only the code fix.

### 6.1: Clean up the branch

**Tool:** `cleanup-investigation.ps1`

```pwsh
.github/skills/fix-flaky-test/cleanup-investigation.ps1 -Message "Fix flaky test: <description>"
```

This restores workflow files from `main` and squashes to a single clean commit.

Then push:
```bash
git push --force-with-lease
```

### 6.2: Open the final PR

**⚠️ Do NOT use the `create-pr` skill or the repository's generic PR template.** Use this specific template:

```bash
gh pr create --repo dotnet/aspire \
  --title "Fix flaky test: <description>" \
  --body "## Flaky Test Fix

### Test
- **Method**: \`<fully qualified test name>\`
- **Issue**: #<issue-number>

### Root Cause
<1-2 sentence description>

### Fix
<1-2 sentence description of what was changed>

### Verification
| Phase | Run | Config | Result |
|-------|-----|--------|--------|
| Reproduction (pre-fix) | [link] | <runners × iters × OSes> | **N/M failed on <OS>** ❌ |
| Verification (post-fix) | [link] | <runners × iters × OSes> | **All N passed on <OS>** ✅ |

**Local runs** (if applicable):
- Pre-fix: <iterations> on <OS> — <pass/fail>
- Post-fix: <iterations> on <OS> — <pass/fail>

> If any OS had zero tests executed, state explicitly and exclude from counts.
> If any step was skipped (e.g. workflow dispatch 403), explain why and provide the manual command.

### Verification Rationale
<Brief explanation: local confidence level, CI scale reasoning>

### Notes
- \`[QuarantinedTest]\` attribute kept — unquarantining happens separately after 21 days of zero failures

---
> **Note:** This PR intentionally does not close #<issue-number>. The test will remain quarantined until stability is confirmed.

---
*This fix was generated using the [fix-flaky-test skill](https://github.com/dotnet/aspire/blob/main/.github/skills/fix-flaky-test/SKILL.md).*"
```

### 6.3: Validate the PR

**Tool:** `validate-flaky-pr.ps1`

```pwsh
.github/skills/fix-flaky-test/validate-flaky-pr.ps1 -PRNumber <number>
```

If any check fails, fix with `gh pr edit` and re-validate.

### 6.4: Monitor CI

The regular CI pipeline runs automatically on the PR. If it fails on your changed files, investigate. Unrelated failures are not your problem.

**Done when:** PR is open, validated, and CI is green (or failures are unrelated).

## Tool Reference

### configure-reproduce.ps1

Patches `reproduce-flaky-tests.yml` with validated test configuration.

```pwsh
.github/skills/fix-flaky-test/configure-reproduce.ps1 `
  -Project <string>       # Test project shortname (required). E.g., "Hosting.Azure"
  -Method <string>        # Test method name (required). Comma-separated for multiple.
  -OS <string>            # Target OSes (default: "ubuntu-latest,windows-latest")
  -Runners <int>          # Runners per OS (default: 3)
  -Iterations <int>       # Iterations per runner (default: 10)
  -FilterType <string>    # "method" (default) or "class"
  -Commit                 # Git add + commit the changes
```

**Exit codes:** 0=success, 1=project not found, 2=YAML not found

### cleanup-investigation.ps1

Restores workflow files and squashes to a single commit.

```pwsh
.github/skills/fix-flaky-test/cleanup-investigation.ps1 `
  -Message <string>       # Commit message (required)
  -Base <string>          # Base branch (default: "main")
```

**Exit codes:** 0=success, 1=precondition failed, 2=commit failed

Does NOT auto-push — you must run `git push --force-with-lease` after.

### validate-flaky-pr.ps1

Checks a PR for common flaky-test-fix mistakes. Report-only.

```pwsh
.github/skills/fix-flaky-test/validate-flaky-pr.ps1 -PRNumber <int>
```

**Checks:** auto-close keywords, required sections, workflow files in diff, QuarantinedTest removal.

**Exit codes:** 0=all pass, 1=failures found, 2=gh auth failed

### run-test-repeatedly.sh / .ps1

Runs a test command repeatedly with process cleanup between iterations.

```bash
# Linux/macOS
.github/skills/fix-flaky-test/run-test-repeatedly.sh -n 20 -- <test command>

# Windows
.github/skills/fix-flaky-test/run-test-repeatedly.ps1 -n 20 -- <test command>
```

**Options:** `-n <count>` (default: 100), `--run-all` (don't stop on first failure)

Detects zero-test runs (exit code 8), timeouts (exit code 124), and test failures.

Results saved to `/tmp/test-results-<timestamp>/` (Linux/macOS) or `$env:TEMP\test-results-<timestamp>\` (Windows).

## REFERENCE.md Triggers

- **Adjusting iteration counts beyond defaults** → consult REFERENCE.md § Verification Scale
- **Unexpected reproduce workflow results** → consult REFERENCE.md § CI Architecture
- **Windows log encoding issues** → consult REFERENCE.md § Windows Encoding
- **Test only fails alongside other tests** → consult REFERENCE.md § Contention Indicators
- **Choosing which OSes to target** → consult REFERENCE.md § OS Targeting
- **Matching test code to known flaky patterns** → consult REFERENCE.md § Flaky Test Patterns

## Response Format

After completing a fix, provide this summary:

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
| Phase | Run | Config | Result |
|-------|-----|--------|--------|
| Reproduction (pre-fix) | [link] | X × Y × Z OSes | N/M failed ❌ |
| Verification (post-fix) | [link] | X × Y × Z OSes | All N passed ✅ |

### Files Changed
- `path/to/file.cs` — description

### Next Steps
- Test remains quarantined — unquarantine after 21 days of zero failures
- Issue #XXXXX remains open
```
