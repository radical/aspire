#!/usr/bin/env pwsh

<#
.SYNOPSIS
    Configures reproduce-flaky-tests.yml for a specific test.

.DESCRIPTION
    Patches the env section of reproduce-flaky-tests.yml with the specified
    test project, filter, OS targets, and iteration counts. Validates that
    the test project exists before patching.

.EXAMPLE
    .github/skills/fix-flaky-test/configure-reproduce.ps1 -Project "Hosting.Azure" -Method "TestMethodName"

.EXAMPLE
    .github/skills/fix-flaky-test/configure-reproduce.ps1 -Project "Hosting.Azure" -Method "TestMethodName" -OS "windows-latest" -Runners 5 -Iterations 5 -Commit
#>

param(
    [Parameter(Mandatory)]
    [string]$Project,

    [Parameter(Mandatory)]
    [string]$Method,

    [string]$OS = "ubuntu-latest,windows-latest",

    [int]$Runners = 3,

    [int]$Iterations = 10,

    [string]$FilterType = "method",

    [switch]$Commit
)

$ErrorActionPreference = 'Stop'

# ---------- locate repo root ----------
$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) {
    Write-Error "Not in a git repository."
    exit 1
}

# ---------- validate project ----------
$projectPath = $null
$candidates = @(
    (Join-Path $repoRoot "tests" "$Project.Tests" "$Project.Tests.csproj"),
    (Join-Path $repoRoot "tests" "Aspire.$Project.Tests" "Aspire.$Project.Tests.csproj")
)

foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        $projectPath = $candidate
        break
    }
}

if (-not $projectPath) {
    Write-Host "ERROR: Test project not found for '$Project'." -ForegroundColor Red
    Write-Host "Searched:"
    foreach ($c in $candidates) { Write-Host "  $c" }
    exit 1
}

Write-Host "Found project: $projectPath" -ForegroundColor Green

# ---------- build filter ----------
$filterFlag = switch ($FilterType) {
    "method" { "--filter-method" }
    "class"  { "--filter-class" }
    default  { Write-Error "Unknown FilterType: $FilterType. Use 'method' or 'class'."; exit 1 }
}

# Support multiple methods (comma-separated)
$methods = $Method -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$filterParts = $methods | ForEach-Object { "$filterFlag `"*.$_`"" }
$testFilter = $filterParts -join ' '

# ---------- locate and patch workflow ----------
$workflowPath = Join-Path $repoRoot ".github" "workflows" "reproduce-flaky-tests.yml"
if (-not (Test-Path $workflowPath)) {
    Write-Error "Workflow file not found: $workflowPath"
    exit 2
}

$content = Get-Content $workflowPath -Raw

# Patch env vars using regex replace (idempotent)
$replacements = @(
    @{ Pattern = '(TEST_PROJECT:\s*")([^"]*)(")';           Value = $Project }
    @{ Pattern = "(TEST_FILTER:\s*')([^']*)(')" ;            Value = $testFilter }
    @{ Pattern = '(TARGET_OSES:\s*")([^"]*)(")';            Value = $OS }
    @{ Pattern = '(RUNNERS_PER_OS:\s*")([^"]*)(")';         Value = "$Runners" }
    @{ Pattern = '(ITERATIONS_PER_RUNNER:\s*")([^"]*)(")';  Value = "$Iterations" }
)

foreach ($r in $replacements) {
    $content = $content -replace $r.Pattern, "`${1}$($r.Value)`${3}"
}

Set-Content -Path $workflowPath -Value $content -NoNewline

# ---------- summary ----------
Write-Host ""
Write-Host "Configured reproduce-flaky-tests.yml:" -ForegroundColor Cyan
Write-Host "  TEST_PROJECT:         $Project"
Write-Host "  TEST_FILTER:          $testFilter"
Write-Host "  TARGET_OSES:          $OS"
Write-Host "  RUNNERS_PER_OS:       $Runners"
Write-Host "  ITERATIONS_PER_RUNNER: $Iterations"
Write-Host "  Total jobs:           $($Runners * ($OS -split ',' | Measure-Object).Count)"
Write-Host ""

# ---------- optionally commit ----------
if ($Commit) {
    git add $workflowPath
    git commit -m "Configure reproduce workflow for $($methods -join ', ')"
    Write-Host "Committed workflow changes." -ForegroundColor Green
}

exit 0
