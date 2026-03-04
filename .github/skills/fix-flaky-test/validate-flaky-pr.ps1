#!/usr/bin/env pwsh

<#
.SYNOPSIS
    Validates a flaky test fix PR for common mistakes.

.DESCRIPTION
    Checks the PR body and diff for issues that have caused problems in past
    flaky test fix PRs. Report-only — does not modify anything.

.EXAMPLE
    .github/skills/fix-flaky-test/validate-flaky-pr.ps1 -PRNumber 12345
#>

param(
    [Parameter(Mandatory)]
    [int]$PRNumber,

    [string]$Repo = "dotnet/aspire"
)

$ErrorActionPreference = 'Stop'

# ---------- check gh auth ----------
$authStatus = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: gh auth failed. Run 'gh auth login' first." -ForegroundColor Red
    exit 2
}

# ---------- fetch PR data ----------
Write-Host "Fetching PR #$PRNumber..." -ForegroundColor Cyan

$prBody = gh pr view $PRNumber --repo $Repo --json body --jq '.body' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Could not fetch PR #$PRNumber from $Repo." -ForegroundColor Red
    exit 2
}

$prDiff = gh pr diff $PRNumber --repo $Repo 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Could not fetch diff for PR #$PRNumber." -ForegroundColor Red
    exit 2
}

# ---------- run checks ----------
$failures = 0

function Test-Check {
    param([string]$Name, [bool]$Pass, [string]$FixHint)

    if ($Pass) {
        Write-Host "  PASS  $Name" -ForegroundColor Green
    } else {
        Write-Host "  FAIL  $Name" -ForegroundColor Red
        Write-Host "        Fix: $FixHint" -ForegroundColor Yellow
        $script:failures++
    }
}

Write-Host ""
Write-Host "Validation Results:" -ForegroundColor Cyan
Write-Host "===================" -ForegroundColor Cyan

# 1. No auto-close keywords
$hasAutoClose = $prBody -match '(?mi)^\s*(fix(es|ed)?|close[sd]?|resolve[sd]?)\s+#'
Test-Check "No auto-close keywords (Fixes/Closes/Resolves #)" `
    (-not $hasAutoClose) `
    "Remove 'Fixes #', 'Closes #', or 'Resolves #' from PR body. Use 'Related to #' instead."

# 2. Required sections
$hasRootCause = $prBody -match 'Root Cause'
Test-Check "Root Cause section present" $hasRootCause `
    "Add a '### Root Cause' section describing why the test was flaky."

$hasVerification = $prBody -match 'Verification'
Test-Check "Verification section present" $hasVerification `
    "Add a '### Verification' section with CI run links and results."

$hasNoCloseNote = $prBody -match 'intentionally does not close'
Test-Check "'Intentionally does not close' note present" $hasNoCloseNote `
    "Add note: 'This PR intentionally does not close #<issue>. The test will remain quarantined...'"

$hasCILinks = $prBody -match 'actions/runs/\d+'
Test-Check "CI run links present" $hasCILinks `
    "Add links to the CI verification runs (e.g., https://github.com/dotnet/aspire/actions/runs/12345)."

# 3. No workflow files in diff
$hasWorkflowDiff = $prDiff -match '(?m)^diff.*\.github/workflows/'
Test-Check "No workflow files in diff" (-not $hasWorkflowDiff) `
    "Remove workflow file changes. Use cleanup-investigation.ps1 to restore them."

# 4. QuarantinedTest not removed
$removesQuarantine = $prDiff -match '(?m)^-.*QuarantinedTest'
Test-Check "QuarantinedTest attribute preserved" (-not $removesQuarantine) `
    "Do not remove [QuarantinedTest]. Unquarantining happens separately after 21 days of stability."

# ---------- summary ----------
Write-Host ""
if ($failures -eq 0) {
    Write-Host "All checks passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "$failures check(s) failed." -ForegroundColor Red
    exit 1
}
