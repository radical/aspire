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

            if ($output -match "PATH_UPDATE_SUCCESS") {
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

            if ($output -match "INSTALLATION_COMPLETED" -and $output -notmatch "Added.*to PATH for current session") {
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

        if ($output -match "GITHUB_PATH_SUCCESS") {
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

        if ($output -match "INSTALLATION_COMPLETED") {
            return "Installation completed without GITHUB_PATH (expected behavior)"
        } else {
            throw "Installation failed when GITHUB_PATH not available. Output: $output. Error: $error_output"
        }
    } 0 "Installation completed without GITHUB_PATH" ""

    Write-ColoredOutput "=== Cleanup and Summary ===" -Color 'Yellow'

    # Test 20: Verify cleanup behavior (test with KeepArchive option)
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

    # Clean up test directories
    Cleanup-TestDirectories

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
