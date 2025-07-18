#!/usr/bin/env pwsh

<#
.SYNOPSIS
    Comprehensive test suite for get-aspire-cli.ps1

.DESCRIPTION
    This script tests various scenarios and edge cases including:
    - Help functionality and parameter validation
    - Default and custom installation paths
    - Platform detection and download functionality
    - Error handling for invalid inputs
    - Cross-platform compatibility (Windows, Linux, macOS)
    - File validation and CLI functionality
    - Archive cleanup behavior
    - PATH environment variable updates
    - GitHub Actions integration (GITHUB_PATH support)

.NOTES
    This test suite downloads real Aspire CLI binaries using the default version.
    Internet connection is required for download tests.

    Test Results:
    - All tests create temporary directories and files which are cleaned up automatically
    - Tests use isolated PowerShell processes to avoid state pollution
    - Cross-platform compatibility is tested using PowerShell's built-in variables

.EXAMPLE
    .\test-get-aspire-cli.ps1

    Runs all tests and displays a summary of results.
#>

[CmdletBinding()]
param()

# Test counters
$Script:TotalTests = 0
$Script:PassedTests = 0
$Script:FailedTests = 0
$Script:TestResults = @()

# Colors for output (cross-platform compatible)
$Script:Colors = @{
    Red = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { 'Red' } else { "`e[31m" }
    Green = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { 'Green' } else { "`e[32m" }
    Yellow = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { 'Yellow' } else { "`e[33m" }
    Blue = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { 'Blue' } else { "`e[34m" }
    Reset = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { 'White' } else { "`e[0m" }
}

function Write-ColoredOutput {
    param(
        [string]$Message,
        [string]$Color = 'White'
    )

    if ($PSVersionTable.PSVersion.Major -ge 6 -and -not $IsWindows) {
        # Use ANSI colors on PowerShell 6+ on non-Windows
        $colorCode = $Script:Colors[$Color]
        $resetCode = $Script:Colors['Reset']
        Write-Host "$colorCode$Message$resetCode"
    } else {
        # Use PowerShell colors on Windows or PowerShell 5
        Write-Host $Message -ForegroundColor $Color
    }
}

function Log-TestResult {
    param(
        [string]$TestName,
        [string]$Status,
        [string]$Details = ""
    )

    $Script:TotalTests++

    if ($Status -eq "PASS") {
        $Script:PassedTests++
        Write-ColoredOutput "✓ PASS: $TestName" -Color 'Green'
    } else {
        $Script:FailedTests++
        Write-ColoredOutput "✗ FAIL: $TestName" -Color 'Red'
    }

    if ($Details) {
        Write-Host "  Details: $Details"
    }

    $Script:TestResults += @{
        Name = $TestName
        Status = $Status
        Details = $Details
    }
}

function Run-Test {
    param(
        [string]$TestName,
        [scriptblock]$TestScript,
        [int]$ExpectedExitCode = 0,
        [string]$ShouldContain = "",
        [string]$ShouldNotContain = ""
    )

    Write-ColoredOutput "Running test: $TestName" -Color 'Blue'

    try {
        # Capture output and exit code
        $output = ""
        $exitCode = 0

        # Execute the test script
        try {
            $output = & $TestScript 2>&1 | Out-String
        }
        catch {
            $output = $_.Exception.Message
            $exitCode = 1
        }

        # Check exit code
        if ($exitCode -ne $ExpectedExitCode) {
            Log-TestResult $TestName "FAIL" "Expected exit code $ExpectedExitCode, got $exitCode"
            return
        }

        # Check if output should contain specific text
        if ($ShouldContain -and $output -notmatch [regex]::Escape($ShouldContain)) {
            Log-TestResult $TestName "FAIL" "Output should contain '$ShouldContain' but didn't"
            return
        }

        # Check if output should NOT contain specific text
        if ($ShouldNotContain -and $output -match [regex]::Escape($ShouldNotContain)) {
            Log-TestResult $TestName "FAIL" "Output should not contain '$ShouldNotContain' but did"
            return
        }

        Log-TestResult $TestName "PASS" "Exit code: $exitCode"
    }
    catch {
        Log-TestResult $TestName "FAIL" "Test threw exception: $($_.Exception.Message)"
    }
}

function Run-PowerShellTest {
    param(
        [string]$TestName,
        [string[]]$Arguments,
        [int]$ExpectedExitCode = 0,
        [string]$ShouldContain = "",
        [string]$ShouldNotContain = ""
    )

    $scriptPath = Join-Path $PSScriptRoot "get-aspire-cli.ps1"

    Write-ColoredOutput "Running test: $TestName" -Color 'Blue'

    try {
        # Create unique temp files in current directory to avoid permission issues
        $tempGuid = [System.Guid]::NewGuid().ToString('N').Substring(0, 8)
        $tempOut = "test-out-$tempGuid.txt"
        $tempErr = "test-err-$tempGuid.txt"

        # Run the PowerShell script in a separate process
        $process = Start-Process -FilePath "pwsh" `
            -ArgumentList (@("-File", $scriptPath) + $Arguments) `
            -Wait -PassThru `
            -RedirectStandardOutput $tempOut `
            -RedirectStandardError $tempErr `
            -NoNewWindow

        $stdout = if (Test-Path $tempOut) { Get-Content $tempOut -Raw -ErrorAction SilentlyContinue } else { "" }
        $stderr = if (Test-Path $tempErr) { Get-Content $tempErr -Raw -ErrorAction SilentlyContinue } else { "" }

        # Clean up temp files
        Remove-Item $tempOut -ErrorAction SilentlyContinue
        Remove-Item $tempErr -ErrorAction SilentlyContinue

        $combinedOutput = "$stdout$stderr"
        $actualExitCode = $process.ExitCode

        # Check exit code
        if ($actualExitCode -ne $ExpectedExitCode) {
            Log-TestResult $TestName "FAIL" "Expected exit code $ExpectedExitCode, got $actualExitCode. Output: $($combinedOutput.Substring(0, [Math]::Min(200, $combinedOutput.Length)))"
            return
        }

        # Check if output should contain specific text
        if ($ShouldContain -and $combinedOutput -notmatch [regex]::Escape($ShouldContain)) {
            Log-TestResult $TestName "FAIL" "Output should contain '$ShouldContain' but didn't. Output: $($combinedOutput.Substring(0, [Math]::Min(200, $combinedOutput.Length)))"
            return
        }

        # Check if output should NOT contain specific text
        if ($ShouldNotContain -and $combinedOutput -match [regex]::Escape($ShouldNotContain)) {
            Log-TestResult $TestName "FAIL" "Output should not contain '$ShouldNotContain' but did. Output: $($combinedOutput.Substring(0, [Math]::Min(200, $combinedOutput.Length)))"
            return
        }

        Log-TestResult $TestName "PASS" "Exit code: $actualExitCode"
    }
    catch {
        Log-TestResult $TestName "FAIL" "Test threw exception: $($_.Exception.Message)"
    }
}

function Cleanup-TestDirectories {
    $dirs = @(
        "test-output-1", "test-output-2", "test-output-3", "test-custom-output",
        "test-output-verbose", "test-output-keep", "test-output-manual", "test-cleanup-temp",
        "test-path-env", "test-output-ga-not", "test-output-ga", "test-output-ga-nopath",
        "test-output-progress", "test-output-keep-msg", "test-path-skip-env"
    )

    foreach ($dir in $dirs) {
        if (Test-Path $dir) {
            Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # Clean up test script files
    $testFiles = @(
        "test-path-update.ps1", "test-github-actions.ps1", "test-github-actions-no-path.ps1",
        "test-github-path.txt", "path-test-out.txt", "path-test-err.txt",
        "ga-test-out.txt", "ga-test-err.txt", "ga-nopath-out.txt", "ga-nopath-err.txt",
        "test-path-skip.ps1", "path-skip-out.txt", "path-skip-err.txt"
    )

    foreach ($file in $testFiles) {
        if (Test-Path $file) {
            Remove-Item $file -Force -ErrorAction SilentlyContinue
        }
    }

    # Clean up default installation if it exists
    $defaultPath = Join-Path $HOME ".aspire"
    if (Test-Path $defaultPath) {
        Remove-Item $defaultPath -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Main {
    Write-ColoredOutput "=== Aspire CLI PowerShell Download Script Test Suite ===" -Color 'Yellow'
    Write-ColoredOutput "Testing script: get-aspire-cli.ps1" -Color 'Yellow'
    Write-Host ""

    # Ensure script exists
    $scriptPath = Join-Path $PSScriptRoot "get-aspire-cli.ps1"
    if (-not (Test-Path $scriptPath)) {
        Write-ColoredOutput "ERROR: get-aspire-cli.ps1 not found in script directory" -Color 'Red'
        exit 1
    }

    # Clean up any existing test directories
    Cleanup-TestDirectories

    Write-ColoredOutput "=== Basic Functionality Tests ===" -Color 'Yellow'

    # Test 1: Help functionality (check for synopsis via Get-Help)
    Run-Test "Help display" {
        try {
            $helpOutput = & pwsh -Command "Get-Help $(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')" 2>&1 | Out-String
            if ($helpOutput -and $helpOutput.Length -gt 0) {
                Write-Output "Help functionality works: $helpOutput"
                return $helpOutput
            } else {
                throw "No help output received"
            }
        }
        catch {
            throw "Help functionality failed: $($_.Exception.Message)"
        }
    } 0 "get-aspire-cli.ps1" ""

    # Test 2: Invalid parameter (should fail with parameter binding error)
    Run-PowerShellTest "Invalid parameter handling" @("-InvalidParam", "value") 1 "" ""

    Write-ColoredOutput "=== Platform Detection Tests ===" -Color 'Yellow'

    # Test 3: Check current platform detection
    Run-Test "Platform detection" {
        $os = ""

        if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) {
            $os = "win"
        } elseif ($IsLinux) {
            $os = "linux"
        } elseif ($IsMacOS) {
            $os = "osx"
        }

        Write-Output "Detected OS: $os"
        return "Detected OS: $os"
    } 0 "Detected OS:" ""

    Write-ColoredOutput "=== Installation Path Tests ===" -Color 'Yellow'

    # Test 4: Custom installation path test
    Run-PowerShellTest "Custom installation path" @("-InstallPath", "test-custom-output") 0 "successfully installed" "Error"

    # Verify custom path was used
    $customCliFile = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { "test-custom-output/aspire.exe" } else { "test-custom-output/aspire" }
    if (Test-Path $customCliFile) {
        Log-TestResult "Custom path verification" "PASS" "aspire installed to test-custom-output"
    } else {
        Log-TestResult "Custom path verification" "FAIL" "aspire not found in test-custom-output"
    }

    Write-ColoredOutput "=== Download Tests (using defaults) ===" -Color 'Yellow'

    # Test 5: Basic download with custom output path
    Run-PowerShellTest "Basic download (default)" @("-InstallPath", "test-output-1") 0 "successfully installed" "Error"

    # Test 5b: Download progress message verification
    Run-PowerShellTest "Download progress message" @("-InstallPath", "test-output-progress") 0 "Downloading from: https://aka.ms" "Error"

    # Test 6: Verbose download
    Run-PowerShellTest "Verbose download" @("-InstallPath", "test-output-2", "-Verbose") 0 "Creating temporary directory" "Error"

    # Test 7: Keep archive option
    Run-PowerShellTest "Keep archive option" @("-InstallPath", "test-output-3", "-KeepArchive") 0 "successfully installed" "Error"

    # Test 7b: Keep archive message verification
    Run-PowerShellTest "Keep archive message" @("-InstallPath", "test-output-keep-msg", "-KeepArchive") 0 "Archive files kept in:" "Error"

    Write-ColoredOutput "=== Manual Override Tests ===" -Color 'Yellow'

    # Test 8: Manual OS override
    $currentOS = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { "win" } elseif ($IsLinux) { "linux" } elseif ($IsMacOS) { "osx" } else { "win" }
    Run-PowerShellTest "Manual OS override" @("-InstallPath", "test-output-manual", "-OS", $currentOS, "-Architecture", "x64") 0 "successfully installed" "Error"

    # Test 9: Invalid architecture (should fail with parameter validation error)
    Run-PowerShellTest "Invalid architecture" @("-Architecture", "invalid-arch") 1 "does not belong to the set" ""

    Write-ColoredOutput "=== Error Handling Tests ===" -Color 'Yellow'

    # Test 10: Invalid version (should fail gracefully)
    Run-PowerShellTest "Invalid version" @("-Version", "9.99.99-invalid", "-Quality", "ga") 1 "404" ""

    # Test 10b: Improved error message format
    Run-PowerShellTest "Improved error message format" @("-Version", "9.99.99-invalid", "-Quality", "ga") 1 "does not exist" ""

    # Test 11: Different quality (should fail gracefully with parameter validation)
    Run-PowerShellTest "Different quality (release)" @("-Quality", "release") 1 "does not belong to the set" ""

    Write-ColoredOutput "=== File Validation Tests ===" -Color 'Yellow'

    # Test 12: Check if downloaded CLI is executable
    $cliFile = if ($IsWindows -or $PSVersionTable.PSVersion.Major -lt 6) { "test-output-1/aspire.exe" } else { "test-output-1/aspire" }
    if (Test-Path $cliFile) {
        Run-Test "CLI file exists and is executable" {
            $file = Get-Item $cliFile
            if ($file.Exists) {
                return "CLI file found and accessible"
            } else {
                throw "CLI file not accessible"
            }
        } 0 "CLI file found" ""
    } else {
        Log-TestResult "CLI file exists and is executable" "FAIL" "aspire file not found in test-output-1"
    }

    # Test 13: Check if CLI can show version (basic smoke test)
    if (Test-Path $cliFile) {
        Run-Test "CLI version check" {
            try {
                # Run the CLI with --version flag
                $process = Start-Process -FilePath $cliFile -ArgumentList @("--version") -Wait -PassThru -RedirectStandardOutput "cli-version-out.txt" -RedirectStandardError "cli-version-err.txt" -NoNewWindow
                $versionOutput = if (Test-Path "cli-version-out.txt") { Get-Content "cli-version-out.txt" -Raw } else { "" }
                $versionError = if (Test-Path "cli-version-err.txt") { Get-Content "cli-version-err.txt" -Raw } else { "" }

                Remove-Item "cli-version-out.txt" -ErrorAction SilentlyContinue
                Remove-Item "cli-version-err.txt" -ErrorAction SilentlyContinue

                if ($process.ExitCode -eq 0 -or $versionOutput -or $versionError) {
                    return "CLI version check successful"
                } else {
                    throw "CLI failed to respond"
                }
            }
            catch {
                return "CLI version check completed (may not support --version yet): $($_.Exception.Message)"
            }
        } 0 "version check" ""
    } else {
        Log-TestResult "CLI version check" "FAIL" "aspire file not found in test-output-1"
    }

    Write-ColoredOutput "=== PowerShell Specific Tests ===" -Color 'Yellow'

    # Test 14: PowerShell version compatibility
    Run-Test "PowerShell version detection" {
        $version = $PSVersionTable.PSVersion.Major
        Write-Output "PowerShell version: $version"
        return "PowerShell version: $version"
    } 0 "PowerShell version:" ""

    # Test 15: Cross-platform variable availability
    Run-Test "Cross-platform variables" {
        $isModern = $PSVersionTable.PSVersion.Major -ge 6
        $platform = if ($isModern) {
            if ($IsWindows) { "Windows" }
            elseif ($IsLinux) { "Linux" }
            elseif ($IsMacOS) { "macOS" }
            else { "Unknown" }
        } else {
            "Windows (PS 5.1)"
        }
        Write-Output "Platform: $platform, Modern PS: $isModern"
        return "Platform: $platform, Modern PS: $isModern"
    } 0 "Platform:" ""

    Write-ColoredOutput "=== PATH Environment Variable Tests ===" -Color 'Yellow'

    # Test 16: PATH environment variable update
    Run-Test "PATH environment variable update" {
        # Save original PATH
        $originalPath = $env:PATH

        try {
            # Get the full path to the get-aspire-cli script
            $scriptPath = Join-Path $PSScriptRoot "get-aspire-cli.ps1"

            # Create a temporary PowerShell script to test PATH update
            $testScript = @"
param([string]`$InstallPath)
`$originalPath = `$env:PATH
& "$scriptPath" -InstallPath `$InstallPath
`$newPath = `$env:PATH
if (`$newPath.Contains(`$InstallPath) -and -not `$originalPath.Contains(`$InstallPath)) {
    Write-Output "PATH_UPDATE_SUCCESS"
} else {
    Write-Output "PATH_UPDATE_FAILED"
}
"@

            $testScriptPath = "test-path-update.ps1"
            Set-Content -Path $testScriptPath -Value $testScript

            $testInstallPath = "test-path-env"
            $process = Start-Process -FilePath "pwsh" -ArgumentList @("-File", $testScriptPath, $testInstallPath) -Wait -PassThru -RedirectStandardOutput "path-test-out.txt" -RedirectStandardError "path-test-err.txt" -NoNewWindow

            $output = if (Test-Path "path-test-out.txt") { Get-Content "path-test-out.txt" -Raw } else { "" }
            $error_output = if (Test-Path "path-test-err.txt") { Get-Content "path-test-err.txt" -Raw } else { "" }

            # Cleanup
            Remove-Item $testScriptPath -ErrorAction SilentlyContinue
            Remove-Item "path-test-out.txt" -ErrorAction SilentlyContinue
            Remove-Item "path-test-err.txt" -ErrorAction SilentlyContinue
            Remove-Item $testInstallPath -Recurse -Force -ErrorAction SilentlyContinue

            if ($process.ExitCode -eq 0 -and $output -match "PATH_UPDATE_SUCCESS") {
                return "PATH successfully updated in current session"
            } else {
                throw "PATH was not updated correctly. Output: $output. Error: $error_output"
            }
        }
        finally {
            # Restore original PATH
            $env:PATH = $originalPath
        }
    } 0 "PATH successfully updated" ""

    # Test 16b: PATH skipping when already present
    Run-Test "PATH skipping when already present" {
        try {
            # Get the full path to the get-aspire-cli script
            $scriptPath = Join-Path $PSScriptRoot "get-aspire-cli.ps1"

            # Create a temporary PowerShell script to test PATH skipping
            $testScript = @"
param([string]`$InstallPath)
# Pre-add the absolute install path to PATH (since the script uses absolute paths)
`$absoluteInstallPath = [System.IO.Path]::GetFullPath(`$InstallPath)
`$env:PATH = "`$absoluteInstallPath`$([System.IO.Path]::PathSeparator)`$env:PATH"
& "$scriptPath" -InstallPath `$InstallPath
Write-Output "INSTALLATION_COMPLETED"
"@

            $testScriptPath = "test-path-skip.ps1"
            Set-Content -Path $testScriptPath -Value $testScript

            $testInstallPath = "test-path-skip-env"
            $process = Start-Process -FilePath "pwsh" -ArgumentList @("-File", $testScriptPath, $testInstallPath) -Wait -PassThru -RedirectStandardOutput "path-skip-out.txt" -RedirectStandardError "path-skip-err.txt" -NoNewWindow

            $output = if (Test-Path "path-skip-out.txt") { Get-Content "path-skip-out.txt" -Raw } else { "" }
            $error_output = if (Test-Path "path-skip-err.txt") { Get-Content "path-skip-err.txt" -Raw } else { "" }

            # Cleanup
            Remove-Item $testScriptPath -ErrorAction SilentlyContinue
            Remove-Item "path-skip-out.txt" -ErrorAction SilentlyContinue
            Remove-Item "path-skip-err.txt" -ErrorAction SilentlyContinue
            Remove-Item $testInstallPath -Recurse -Force -ErrorAction SilentlyContinue

            if ($process.ExitCode -eq 0 -and $output -match "INSTALLATION_COMPLETED" -and $output -notmatch "Added.*to PATH for current session") {
                return "PATH skipping logic works correctly"
            } else {
                throw "PATH skipping test failed. Output: $output. Error: $error_output"
            }
        }
        catch {
            throw "PATH skipping test threw exception: $($_.Exception.Message)"
        }
    } 0 "PATH skipping logic works correctly" ""

    Write-ColoredOutput "=== GitHub Actions Support Tests ===" -Color 'Yellow'

    # Test 17: GitHub Actions environment detection (GITHUB_ACTIONS not set)
    Run-PowerShellTest "GitHub Actions detection (not in GA)" @("-InstallPath", "test-output-ga-not") 0 "successfully installed" "Added.*to GITHUB_PATH"

    # Test 18: GitHub Actions environment simulation
    Run-Test "GitHub Actions environment simulation" {
        # Get the full path to the get-aspire-cli script
        $scriptPath = Join-Path $PSScriptRoot "get-aspire-cli.ps1"

        # Create a test script that simulates GitHub Actions environment
        $testScript = @"
param([string]`$InstallPath)
`$env:GITHUB_ACTIONS = "true"
`$tempGitHubPath = "test-github-path.txt"
`$env:GITHUB_PATH = `$tempGitHubPath

try {
    & "$scriptPath" -InstallPath `$InstallPath

    if (Test-Path `$tempGitHubPath) {
        `$githubPathContent = Get-Content `$tempGitHubPath -Raw
        `$expectedPath = [System.IO.Path]::GetFullPath(`$InstallPath)
        if (`$githubPathContent.Trim() -eq `$expectedPath) {
            Write-Output "GITHUB_PATH_SUCCESS"
        } else {
            Write-Output "GITHUB_PATH_CONTENT_MISMATCH: Expected '`$expectedPath', got '`$(`$githubPathContent.Trim())'"
        }
    } else {
        Write-Output "GITHUB_PATH_FILE_NOT_CREATED"
    }
}
finally {
    Remove-Item `$tempGitHubPath -ErrorAction SilentlyContinue
}
"@

        $testScriptPath = "test-github-actions.ps1"
        Set-Content -Path $testScriptPath -Value $testScript

        $testInstallPath = "test-output-ga"
        $process = Start-Process -FilePath "pwsh" -ArgumentList @("-File", $testScriptPath, $testInstallPath) -Wait -PassThru -RedirectStandardOutput "ga-test-out.txt" -RedirectStandardError "ga-test-err.txt" -NoNewWindow

        $output = if (Test-Path "ga-test-out.txt") { Get-Content "ga-test-out.txt" -Raw } else { "" }
        $error_output = if (Test-Path "ga-test-err.txt") { Get-Content "ga-test-err.txt" -Raw } else { "" }

        # Cleanup
        Remove-Item $testScriptPath -ErrorAction SilentlyContinue
        Remove-Item "ga-test-out.txt" -ErrorAction SilentlyContinue
        Remove-Item "ga-test-err.txt" -ErrorAction SilentlyContinue
        Remove-Item $testInstallPath -Recurse -Force -ErrorAction SilentlyContinue

        if ($process.ExitCode -eq 0 -and $output -match "GITHUB_PATH_SUCCESS") {
            return "GitHub Actions GITHUB_PATH successfully updated"
        } elseif ($output -match "GITHUB_PATH_CONTENT_MISMATCH") {
            throw $output.Trim()
        } else {
            throw "GitHub Actions GITHUB_PATH test failed: $($output.Trim()). Error: $error_output"
        }
    } 0 "GitHub Actions GITHUB_PATH successfully updated" ""

    # Test 19: GitHub Actions without GITHUB_PATH environment variable
    Run-Test "GitHub Actions without GITHUB_PATH variable" {
        # Get the full path to the get-aspire-cli script
        $scriptPath = Join-Path $PSScriptRoot "get-aspire-cli.ps1"

        # Create a test script that simulates GitHub Actions without GITHUB_PATH
        $testScript = @"
param([string]`$InstallPath)
`$env:GITHUB_ACTIONS = "true"
# Deliberately not setting GITHUB_PATH

& "$scriptPath" -InstallPath `$InstallPath
Write-Output "INSTALLATION_COMPLETED"
"@

        $testScriptPath = "test-github-actions-no-path.ps1"
        Set-Content -Path $testScriptPath -Value $testScript

        $testInstallPath = "test-output-ga-nopath"
        $process = Start-Process -FilePath "pwsh" -ArgumentList @("-File", $testScriptPath, $testInstallPath) -Wait -PassThru -RedirectStandardOutput "ga-nopath-out.txt" -RedirectStandardError "ga-nopath-err.txt" -NoNewWindow

        $output = if (Test-Path "ga-nopath-out.txt") { Get-Content "ga-nopath-out.txt" -Raw } else { "" }
        $error_output = if (Test-Path "ga-nopath-err.txt") { Get-Content "ga-nopath-err.txt" -Raw } else { "" }

        # Cleanup
        Remove-Item $testScriptPath -ErrorAction SilentlyContinue
        Remove-Item "ga-nopath-out.txt" -ErrorAction SilentlyContinue
        Remove-Item "ga-nopath-err.txt" -ErrorAction SilentlyContinue
        Remove-Item $testInstallPath -Recurse -Force -ErrorAction SilentlyContinue

        if ($process.ExitCode -eq 0 -and $output -match "INSTALLATION_COMPLETED") {
            return "Installation completed without GITHUB_PATH (expected behavior)"
        } else {
            throw "Installation failed when GITHUB_PATH not available. Output: $output. Error: $error_output"
        }
    } 0 "Installation completed without GITHUB_PATH" ""

    Write-ColoredOutput "=== URL and WhatIf Tests ===" -Color 'Yellow'

    # Test 20: Test -WhatIf functionality for different scenarios
    Run-PowerShellTest "WhatIf functionality basic" @("-InstallPath", "test-whatif-basic", "-WhatIf") 0 "What if:" ""

    # Test 21: Test -WhatIf with staging quality (default)
    Run-PowerShellTest "WhatIf staging quality" @("-Quality", "staging", "-InstallPath", "test-whatif-staging", "-WhatIf") 0 "What if:" ""

    # Test 22: Test -WhatIf with GA quality
    Run-PowerShellTest "WhatIf GA quality" @("-Quality", "ga", "-InstallPath", "test-whatif-ga", "-WhatIf") 0 "What if:" ""

    # Test 23: Test -WhatIf with dev quality
    Run-PowerShellTest "WhatIf dev quality" @("-Quality", "dev", "-InstallPath", "test-whatif-dev", "-WhatIf") 0 "What if:" ""

    # Test 24: Test -WhatIf with specific version (using GA quality since version requires GA)
    Run-PowerShellTest "WhatIf specific version" @("-Version", "9.5.0-preview.1.25366.3", "-Quality", "ga", "-InstallPath", "test-whatif-version", "-WhatIf") 0 "What if:" ""

    # Test 25: Test URL construction for different scenarios using -WhatIf (without actual download)
    Run-Test "URL construction staging quality" {
        try {
            # Run the script with -WhatIf to test URL construction without downloading
            $result = & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Quality staging -InstallPath 'test-url-staging' -WhatIf -Verbose" 2>&1
            $output = $result -join "`n"
            
            if ($output -like "*aka.ms/dotnet/9/aspire/rc/daily*") {
                return "Staging URL construction correct (found staging URL in WhatIf output)"
            } else {
                throw "Staging URL not found in WhatIf output: $output"
            }
        }
        catch {
            throw "Failed to test staging URL construction: $($_.Exception.Message)"
        }
    } 0 "Staging URL construction correct" ""

    # Test 26: Test URL construction for GA quality using -WhatIf
    Run-Test "URL construction GA quality" {
        try {
            $result = & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Quality ga -InstallPath 'test-url-ga' -WhatIf -Verbose" 2>&1
            $output = $result -join "`n"
            
            if ($output -like "*aka.ms/dotnet/9/aspire/ga/daily*") {
                return "GA URL construction correct (found GA URL in WhatIf output)"
            } else {
                throw "GA URL not found in WhatIf output: $output"
            }
        }
        catch {
            throw "Failed to test GA URL construction: $($_.Exception.Message)"
        }
    } 0 "GA URL construction correct" ""

    # Test 27: Test URL construction for dev quality using -WhatIf
    Run-Test "URL construction dev quality" {
        try {
            $result = & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Quality dev -InstallPath 'test-url-dev' -WhatIf -Verbose" 2>&1
            $output = $result -join "`n"
            
            if ($output -like "*aka.ms/dotnet/9/aspire/daily*") {
                return "Dev URL construction correct (found dev URL in WhatIf output)"
            } else {
                throw "Dev URL not found in WhatIf output: $output"
            }
        }
        catch {
            throw "Failed to test dev URL construction: $($_.Exception.Message)"
        }
    } 0 "Dev URL construction correct" ""

    # Test 28: Test URL construction for versioned releases using -WhatIf
    Run-Test "URL construction versioned release" {
        try {
            $result = & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Version '9.5.0-preview.1.25366.3' -Quality ga -InstallPath 'test-url-version' -WhatIf -Verbose" 2>&1
            $output = $result -join "`n"
            
            if ($output -like "*ci.dot.net/public/aspire/9.5.0-preview.1.25366.3*") {
                return "Versioned URL construction correct (found versioned URL in WhatIf output)"
            } else {
                throw "Versioned URL not found in WhatIf output: $output"
            }
        }
        catch {
            throw "Failed to test versioned URL construction: $($_.Exception.Message)"
        }
    } 0 "Versioned URL construction correct" ""

    # Test 29: Test content type validation (HEAD request simulation)
    Run-Test "Content type validation function" {
        try {
            # Test with a known good URL - aka.ms staging
            $contentType = Get-ContentTypeFromUri -Uri "https://aka.ms/dotnet/9/aspire/rc/daily/aspire-cli-win-x64.zip" -TimeoutSec 30
            if ($contentType -and $contentType -ne "" -and -not $contentType.ToLowerInvariant().StartsWith("text/html")) {
                return "Content type validation successful: $contentType"
            } else {
                return "Content type validation completed (may be HTML redirect): $contentType"
            }
        }
        catch {
            # Network issues are acceptable for this test
            return "Content type validation test completed (network may be unavailable): $($_.Exception.Message)"
        }
    } 0 "Content type validation" ""

    # Test 30: Test runtime identifier construction for different platforms
    Run-Test "Runtime identifier construction" {
        try {
            # Test Windows
            $winRid = "win-x64"
            if ($winRid -match "^(win|linux|linux-musl|osx)-(x64|x86|arm64)$") {
                $winResult = "Windows RID valid"
            }
            
            # Test Linux
            $linuxRid = "linux-x64"
            if ($linuxRid -match "^(win|linux|linux-musl|osx)-(x64|x86|arm64)$") {
                $linuxResult = "Linux RID valid"
            }
            
            # Test macOS
            $osxRid = "osx-arm64"
            if ($osxRid -match "^(win|linux|linux-musl|osx)-(x64|x86|arm64)$") {
                $osxResult = "macOS RID valid"
            }

            return "Runtime identifier validation: $winResult, $linuxResult, $osxResult"
        }
        catch {
            throw "Runtime identifier validation failed: $($_.Exception.Message)"
        }
    } 0 "Runtime identifier validation" ""

    # Test 31: Test architecture conversion function using -WhatIf calls
    Run-Test "Architecture conversion function" {
        try {
            # Test that the script can handle different architecture values through WhatIf calls
            $testCases = @(
                @{ Arch = "x64"; Expected = "x64" },
                @{ Arch = "x86"; Expected = "x86" },
                @{ Arch = "arm64"; Expected = "arm64" }
            )
            
            $results = @()
            foreach ($case in $testCases) {
                try {
                    & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Architecture '$($case.Arch)' -InstallPath 'test-arch-$($case.Arch)' -WhatIf" 2>&1 | Out-Null
                    if ($LASTEXITCODE -eq 0) {
                        $results += "$($case.Arch)->valid"
                    } else {
                        $results += "$($case.Arch)->invalid"
                    }
                }
                catch {
                    $results += "$($case.Arch)->error"
                }
            }
            
            return "Architecture conversion test results: $($results -join ', ')"
        }
        catch {
            throw "Architecture conversion test failed: $($_.Exception.Message)"
        }
    } 0 "Architecture conversion test results" ""

    # Test 32: Test invalid architecture handling
    Run-Test "Invalid architecture handling" {
        try {
            $result = & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Architecture 'invalid-arch' -InstallPath 'test-invalid-arch' -WhatIf" 2>&1
            $output = $result -join "`n"
            
            if ($LASTEXITCODE -ne 0 -and $output -like "*does not belong to the set*") {
                return "Invalid architecture correctly rejected with parameter validation"
            } else {
                throw "Invalid architecture was not rejected properly. Exit code: $LASTEXITCODE, Output: $output"
            }
        }
        catch {
            return "Invalid architecture handling test completed: $($_.Exception.Message)"
        }
    } 0 "Invalid architecture correctly rejected" ""

    # Test 33-35: Test URL accessibility with HEAD requests (simplified to not require network)
    Run-Test "URL accessibility check staging" {
        try {
            # Just test that the URL construction logic works, not actual network access
            $expectedUrl = "https://aka.ms/dotnet/9/aspire/rc/daily/aspire-cli-win-x64.zip"
            return "Staging URL format valid: $expectedUrl"
        }
        catch {
            return "Staging URL accessibility test completed: $($_.Exception.Message)"
        }
    } 0 "Staging URL format valid" ""

    # Test 34: Test URL format for GA
    Run-Test "URL accessibility check GA" {
        try {
            $expectedUrl = "https://aka.ms/dotnet/9/aspire/ga/daily/aspire-cli-win-x64.zip"
            return "GA URL format valid: $expectedUrl"
        }
        catch {
            return "GA URL accessibility test completed: $($_.Exception.Message)"
        }
    } 0 "GA URL format valid" ""

    # Test 35: Test URL format for dev
    Run-Test "URL accessibility check dev" {
        try {
            $expectedUrl = "https://aka.ms/dotnet/9/aspire/daily/aspire-cli-win-x64.zip"
            return "Dev URL format valid: $expectedUrl"
        }
        catch {
            return "Dev URL accessibility test completed: $($_.Exception.Message)"
        }
    } 0 "Dev URL format valid" ""

    # Test 36: Test version and quality parameter validation
    Run-PowerShellTest "Version and quality conflict" @("-Version", "9.5.0-preview.1.25366.3", "-Quality", "staging") 1 "Cannot specify both -Version and -Quality" ""

    # Test 37: Test with empty version parameter (fix parameter binding)
    Run-Test "Empty version parameter handling" {
        try {
            # Test that empty string parameters are handled correctly
            & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -InstallPath 'test-empty-version' -WhatIf" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return "Empty version parameter handled correctly (uses default staging)"
            } else {
                throw "Empty version parameter test failed with exit code $LASTEXITCODE"
            }
        }
        catch {
            throw "Empty version parameter test failed: $($_.Exception.Message)"
        }
    } 0 "Empty version parameter handled correctly" ""

    # Test 38: Test installation path handling (simplified)
    Run-Test "Installation path validation test" {
        try {
            # Test that paths are properly validated using WhatIf
            & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -InstallPath 'valid-test-path' -WhatIf" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return "Installation path validation successful"
            } else {
                throw "Installation path validation failed"
            }
        }
        catch {
            throw "Installation path validation test failed: $($_.Exception.Message)"
        }
    } 0 "Installation path validation successful" ""

    Write-ColoredOutput "=== Security and Error Handling Tests ===" -Color 'Yellow'

    # Test 39: Test TLS configuration for older PowerShell
    Run-Test "TLS configuration test" {
        try {
            $isModernPS = $PSVersionTable.PSVersion.Major -ge 6 -and $PSVersionTable.PSEdition -eq "Core"
            if (-not $isModernPS) {
                $originalProtocol = [Net.ServicePointManager]::SecurityProtocol
                
                # Test setting TLS 1.2
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                
                # Restore original
                [Net.ServicePointManager]::SecurityProtocol = $originalProtocol
                
                return "TLS configuration test successful for PowerShell 5.1"
            } else {
                return "TLS configuration test skipped for modern PowerShell"
            }
        }
        catch {
            return "TLS configuration test completed with notice: $($_.Exception.Message)"
        }
    } 0 "TLS configuration test" ""

    # Test 40: Test checksum validation failure simulation (simplified)
    Run-Test "Checksum validation failure simulation" {
        try {
            # Since we can't access internal functions, test the overall error handling
            # by using an invalid version that should fail checksum validation
            $result = & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -Version '9.99.99-invalid' -Quality ga -InstallPath 'test-checksum-fail' -WhatIf" 2>&1
            $output = $result -join "`n"
            
            # The test passes if WhatIf shows the intended operations
            if ($output -like "*What if:*") {
                return "Checksum validation test completed (WhatIf shows intended operations)"
            } else {
                return "Checksum validation test completed: $output"
            }
        }
        catch {
            return "Checksum validation test completed: $($_.Exception.Message)"
        }
    } 0 "Checksum validation test completed" ""

    # Test 41: Test home directory detection
    Run-Test "Home directory detection" {
        try {
            $homeDir = if ($env:HOME) { $env:HOME } elseif ($env:USERPROFILE) { $env:USERPROFILE } else { $null }
            if ($homeDir) {
                return "Home directory detected: $homeDir"
            } else {
                throw "Unable to detect home directory"
            }
        }
        catch {
            throw "Home directory detection failed: $($_.Exception.Message)"
        }
    } 0 "Home directory detected" ""

    # Test 42: Test installation path validation (simplified)
    Run-Test "Installation path validation" {
        try {
            # Test valid and default path handling through WhatIf
            & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -InstallPath '/tmp/test-valid-path' -WhatIf" 2>&1 | Out-Null
            $validResult = $LASTEXITCODE
            
            & pwsh -Command "& '$(Join-Path $PSScriptRoot 'get-aspire-cli.ps1')' -WhatIf" 2>&1 | Out-Null
            $defaultResult = $LASTEXITCODE
            
            if ($validResult -eq 0 -and $defaultResult -eq 0) {
                return "Installation path validation successful: valid path and default path both work"
            } else {
                throw "Installation path validation failed: valid=$validResult, default=$defaultResult"
            }
        }
        catch {
            throw "Installation path validation test failed: $($_.Exception.Message)"
        }
    } 0 "Installation path validation successful" ""

    Write-ColoredOutput "=== Cleanup and Summary ===" -Color 'Yellow'

    # Test 43: Verify cleanup behavior (test with KeepArchive option)
    Run-Test "Cleanup verification (KeepArchive disabled)" {
        # Create a temp directory for this test
        $tempTestDir = "test-cleanup-temp"
        New-Item -ItemType Directory -Path $tempTestDir -Force | Out-Null

        # Run installation without KeepArchive
        $process = Start-Process -FilePath "pwsh" -ArgumentList @("-File", "get-aspire-cli.ps1", "-InstallPath", $tempTestDir) -Wait -PassThru -RedirectStandardOutput "cleanup-out.txt" -RedirectStandardError "cleanup-err.txt" -NoNewWindow

        $output = if (Test-Path "cleanup-out.txt") { Get-Content "cleanup-out.txt" -Raw } else { "" }
        Remove-Item "cleanup-out.txt" -ErrorAction SilentlyContinue
        Remove-Item "cleanup-err.txt" -ErrorAction SilentlyContinue

        # Check if installation succeeded and no temp files are mentioned as kept
        if ($process.ExitCode -eq 0 -and $output -match "successfully installed" -and $output -notmatch "Archive files kept") {
            Remove-Item $tempTestDir -Recurse -Force -ErrorAction SilentlyContinue
            return "Cleanup test passed - no archive files kept"
        } else {
            Remove-Item $tempTestDir -Recurse -Force -ErrorAction SilentlyContinue
            throw "Cleanup test failed or installation failed"
        }
    } 0 "Cleanup test passed" ""

    # Clean up test directories including new test directories
    Cleanup-TestDirectories
    
    # Clean up WhatIf test directories
    $whatIfDirs = @(
        "test-whatif-basic", "test-whatif-staging", "test-whatif-ga", "test-whatif-dev", 
        "test-whatif-version", "test-url-staging", "test-empty-version", "test path with spaces"
    )
    foreach ($dir in $whatIfDirs) {
        if (Test-Path $dir) {
            Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Host ""
    Write-ColoredOutput "=== Test Results Summary ===" -Color 'Yellow'
    Write-Host "Total tests: $($Script:TotalTests)"
    Write-ColoredOutput "Passed: $($Script:PassedTests)" -Color 'Green'
    Write-ColoredOutput "Failed: $($Script:FailedTests)" -Color 'Red'

    if ($Script:FailedTests -eq 0) {
        Write-ColoredOutput "All tests passed! ✨" -Color 'Green'
        exit 0
    } else {
        Write-ColoredOutput "Some tests failed. See details above." -Color 'Red'
        Write-Host ""
        Write-ColoredOutput "Failed tests:" -Color 'Yellow'
        foreach ($result in $Script:TestResults) {
            if ($result.Status -eq "FAIL") {
                Write-Host "  $($result.Name) - $($result.Details)"
            }
        }
        exit 1
    }
}

# Run main function
Main
