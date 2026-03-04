#!/usr/bin/env pwsh

<#
.SYNOPSIS
    Cleans up investigation artifacts and squashes to a single fix commit.

.DESCRIPTION
    Restores workflow files from the base branch, stages all changes, and
    squashes everything into a single commit. Verifies the result is clean
    before completing.

.EXAMPLE
    .github/skills/fix-flaky-test/cleanup-investigation.ps1 -Message "Fix flaky test: use thread-safe collection in TestFake"

.EXAMPLE
    .github/skills/fix-flaky-test/cleanup-investigation.ps1 -Message "Fix flaky test: description" -Base "release/13.2"
#>

param(
    [Parameter(Mandatory)]
    [string]$Message,

    [string]$Base = "main"
)

$ErrorActionPreference = 'Stop'

# ---------- preconditions ----------
$currentBranch = git rev-parse --abbrev-ref HEAD 2>$null
if ($currentBranch -eq $Base) {
    Write-Host "ERROR: Cannot clean up while on '$Base'. Switch to your fix branch first." -ForegroundColor Red
    exit 1
}

$mergeBase = git merge-base HEAD $Base 2>$null
if (-not $mergeBase) {
    Write-Host "ERROR: Could not find merge base with '$Base'. Is '$Base' a valid branch?" -ForegroundColor Red
    exit 1
}

$commitCount = (git rev-list --count "$mergeBase..HEAD")
if ($commitCount -eq 0) {
    Write-Host "ERROR: No commits beyond '$Base'. Nothing to clean up." -ForegroundColor Red
    exit 1
}

# ---------- restore workflow files ----------
$workflowFiles = @(
    ".github/workflows/ci.yml",
    ".github/workflows/reproduce-flaky-tests.yml"
)

Write-Host "Restoring workflow files from '$Base'..." -ForegroundColor Cyan
foreach ($f in $workflowFiles) {
    $fullPath = Join-Path (git rev-parse --show-toplevel) $f
    if (Test-Path $fullPath) {
        git checkout $Base -- $f 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WARNING: Could not restore $f from $Base (may not exist on base)." -ForegroundColor Yellow
        }
    }
}
git add .github/workflows/ 2>$null

# ---------- squash ----------
Write-Host "Squashing $commitCount commit(s) into one..." -ForegroundColor Cyan
git add -A
git reset --soft $mergeBase
git commit -m $Message

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Commit failed. Run 'git reset --hard HEAD' to recover." -ForegroundColor Red
    exit 2
}

# ---------- verify ----------
$workflowDiff = git diff "$Base" -- .github/workflows/ 2>$null
if ($workflowDiff) {
    Write-Host "WARNING: Workflow files still differ from '$Base':" -ForegroundColor Yellow
    Write-Host $workflowDiff
    Write-Host ""
    Write-Host "You may need to manually verify these changes are intentional." -ForegroundColor Yellow
}

$finalCount = (git rev-list --count "$mergeBase..HEAD")
Write-Host ""
Write-Host "Cleanup complete:" -ForegroundColor Green
Write-Host "  Branch:  $currentBranch"
Write-Host "  Commits: $finalCount beyond $Base"
Write-Host "  Message: $Message"
Write-Host ""
Write-Host "Next: run 'git push --force-with-lease' then open the PR." -ForegroundColor Cyan

exit 0
