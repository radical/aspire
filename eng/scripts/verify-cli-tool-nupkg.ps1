<#
.SYNOPSIS
    Verify that CLI dotnet-tool nupkgs are valid.

.DESCRIPTION
    This script:
    1. Finds the RID-specific tool nupkg (Aspire.Cli.<rid>.*.nupkg)
    2. Validates the nupkg size is above a minimum threshold
    3. Extracts the package and verifies the aspire binary is present
    4. Verifies the binary OS format and CPU architecture match the target RID
       (PE Machine type on Windows, ELF e_machine on Linux, Mach-O cputype on macOS)
    5. Checks the primary pointer package (Aspire.Cli.*.nupkg) exists
    6. (Optional) Installs the tool locally and runs aspire --version + aspire new

.PARAMETER PackagesDir
    Path to the directory containing nupkg files

.PARAMETER Rid
    Runtime identifier (e.g., win-x64, win-arm64)

.PARAMETER RunFunctionalTests
    When set, installs the tool from the local packages and runs functional checks
    (aspire --version, aspire new). Only use for RIDs that can execute on the agent.

.PARAMETER VerifySignature
    When set, verifies that both the RID-specific nupkg and the pointer package
    contain a .signature.p7s entry (NuGet package signature smoke test).

.EXAMPLE
    .\verify-cli-tool-nupkg.ps1 -PackagesDir "artifacts\packages\Release" -Rid "win-x64"
.EXAMPLE
    .\verify-cli-tool-nupkg.ps1 -PackagesDir "artifacts\packages\Release" -Rid "linux-x64" -RunFunctionalTests
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PackagesDir,

    [Parameter(Mandatory = $true)]
    [string]$Rid,

    [switch]$RunFunctionalTests,

    [switch]$VerifySignature
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$msg) Write-Host "`u{25B6} $msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$msg) Write-Host "`u{2705} $msg" -ForegroundColor Green }
function Write-Err   { param([string]$msg) Write-Host "`u{274C} $msg" -ForegroundColor Red }

function Test-NupkgSignature {
    param([string]$NupkgPath)
    # A NuGet-signed nupkg contains a .signature.p7s entry inside the zip archive.
    try {
        $zip = [System.IO.Compression.ZipFile]::OpenRead($NupkgPath)
        try {
            $hasSig = $zip.Entries | Where-Object { $_.FullName -eq '.signature.p7s' }
            return [bool]$hasSig
        } finally {
            $zip.Dispose()
        }
    } catch {
        Write-Err "Failed to open nupkg as zip: $_"
        return $false
    }
}

# Minimum expected nupkg size in bytes (5 MB — NativeAOT binary should be large)
$MinNupkgSize = 5MB

$extractDir = $null
$toolInstallDir = $null

Add-Type -AssemblyName System.IO.Compression.FileSystem

try {
    Write-Host ""
    Write-Host "=========================================="
    Write-Host "  Aspire CLI Tool Nupkg Verification"
    Write-Host "=========================================="
    Write-Host "  Packages dir:     $PackagesDir"
    Write-Host "  RID:              $Rid"
    Write-Host "  Functional tests: $RunFunctionalTests"
    Write-Host "  Verify signature: $VerifySignature"
    Write-Host "=========================================="
    Write-Host ""

    # Resolve the effective packages source directory (prefer Shipping/ when present)
    $effectiveDir = if (Test-Path (Join-Path $PackagesDir "Shipping")) {
        Join-Path $PackagesDir "Shipping"
    } else {
        $PackagesDir
    }
    Write-Step "Effective source dir: $effectiveDir"

    # Step 1: Find the RID-specific nupkg
    Write-Step "Looking for RID-specific nupkg matching Aspire.Cli.$Rid.*.nupkg ..."
    $ridNupkg = Get-ChildItem -Path $effectiveDir -Filter "Aspire.Cli.$Rid.*.nupkg" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '\.symbols\.' } |
        Select-Object -First 1

    if (-not $ridNupkg) {
        Write-Err "No RID-specific nupkg found for $Rid in $effectiveDir"
        Write-Host "Contents of packages directory:"
        Get-ChildItem -Path $effectiveDir -Filter "*.nupkg" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $($_.Name)" }
        exit 1
    }

    Write-Ok "Found: $($ridNupkg.Name)"

    # Step 2: Check nupkg size
    $nupkgSize = $ridNupkg.Length
    $sizeMB = [math]::Round($nupkgSize / 1MB, 1)
    Write-Step "Checking nupkg size: $sizeMB MB ($nupkgSize bytes)"

    if ($nupkgSize -lt $MinNupkgSize) {
        Write-Err "Nupkg is too small ($nupkgSize bytes, minimum $MinNupkgSize bytes). The NativeAOT binary may be missing."
        exit 1
    }
    Write-Ok "Size check passed ($sizeMB MB)"

    # Step 3: Extract and verify the binary
    $extractDir = Join-Path ([System.IO.Path]::GetTempPath()) "aspire-tool-verify-$([System.IO.Path]::GetRandomFileName())"
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

    Write-Step "Extracting nupkg to verify contents..."
    # nupkg is a zip file
    $zipPath = Join-Path $extractDir "$($ridNupkg.BaseName).zip"
    Copy-Item $ridNupkg.FullName $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

    # The tool binary is at tools/net10.0/<rid>/aspire.exe (or aspire on Unix)
    $binaryName = if ($Rid -like 'win-*') { "aspire.exe" } else { "aspire" }
    $toolBinary = Get-ChildItem -Path $extractDir -Recurse -Filter $binaryName -ErrorAction SilentlyContinue | Select-Object -First 1

    if (-not $toolBinary) {
        Write-Err "Could not find '$binaryName' inside the nupkg"
        Write-Host "Nupkg contents:"
        Get-ChildItem -Path $extractDir -Recurse | Select-Object -First 20 | ForEach-Object { Write-Host "  $($_.FullName.Substring($extractDir.Length))" }
        exit 1
    }
    Write-Ok "Found binary: $($toolBinary.FullName.Substring($extractDir.Length))"

    # Step 4: Verify binary OS and architecture match the target RID
    $expectedArch = $Rid.Split('-')[-1]  # x64 or arm64 (works for linux-musl-x64 too)

    if ($Rid -like 'win-*') {
        # Read PE Machine type from header via BitConverter
        Write-Step "Checking PE binary architecture (expected: $expectedArch)..."
        $bytes = [System.IO.File]::ReadAllBytes($toolBinary.FullName)
        $peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
        $machine = [BitConverter]::ToUInt16($bytes, $peOffset + 4)

        $expectedMachine = switch ($expectedArch) {
            'x64'   { 0x8664 }  # IMAGE_FILE_MACHINE_AMD64
            'arm64' { 0xAA64 }  # IMAGE_FILE_MACHINE_ARM64
            default { Write-Err "Unknown Windows architecture: $expectedArch"; exit 1 }
        }

        if ($machine -ne $expectedMachine) {
            Write-Err "PE Machine mismatch. Expected: 0x$($expectedMachine.ToString('X4')) ($expectedArch), Got: 0x$($machine.ToString('X4'))"
            exit 1
        }
        Write-Ok "PE binary architecture: 0x$($machine.ToString('X4')) ($expectedArch)"
    }
    else {
        # On Linux and macOS, use `file` to verify binary format and architecture
        Write-Step "Checking binary format with 'file' (expected arch: $expectedArch)..."
        if (-not (Get-Command file -ErrorAction SilentlyContinue)) {
            Write-Err "'file' command not found. Cannot verify binary architecture for $Rid."
            exit 1
        }

        $fileOutput = & file -b $toolBinary.FullName 2>&1
        Write-Step "file output: $fileOutput"

        $expectedPattern = switch ($Rid) {
            'linux-x64'       { 'ELF 64-bit.*x86-64' }
            'linux-arm64'     { 'ELF 64-bit.*ARM aarch64' }
            'linux-musl-x64'  { 'ELF 64-bit.*x86-64' }
            'osx-x64'         { 'Mach-O 64-bit.*x86_64' }
            'osx-arm64'       { 'Mach-O 64-bit.*arm64' }
            default           { $null }
        }

        if (-not $expectedPattern) {
            Write-Step "No known file signature pattern for RID '$Rid', skipping architecture check"
        }
        elseif ($fileOutput -notmatch $expectedPattern) {
            Write-Err "Binary format mismatch for $Rid. Expected pattern: '$expectedPattern', Got: '$fileOutput'"
            exit 1
        }
        else {
            Write-Ok "Binary format and architecture verified for $Rid"
        }
    }

    # Step 4b: Verify RID-specific nupkg is signed (smoke test)
    if ($VerifySignature) {
        Write-Step "Checking RID-specific nupkg signature..."
        if (-not (Test-NupkgSignature $ridNupkg.FullName)) {
            Write-Err "RID-specific nupkg $($ridNupkg.Name) is NOT signed (no .signature.p7s)"
            exit 1
        }
        Write-Ok "RID-specific nupkg is signed"
    }

    # Step 5: Verify the pointer/shim package exists
    Write-Step "Looking for primary pointer package (Aspire.Cli.*.nupkg, not RID-specific)..."
    $primaryNupkg = Get-ChildItem -Path $effectiveDir -Filter "Aspire.Cli.*.nupkg" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '\.symbols\.' -and $_.Name -notmatch 'Aspire\.Cli\.(win|linux|osx|musl)-' } |
        Select-Object -First 1

    if (-not $primaryNupkg) {
        Write-Err "No primary pointer package found (Aspire.Cli.*.nupkg without RID prefix)"
        Write-Host "All Aspire.Cli nupkgs in ${effectiveDir}:"
        Get-ChildItem -Path $effectiveDir -Filter "Aspire.Cli*.nupkg" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $($_.Name)" }
        exit 1
    }
    Write-Ok "Found primary: $($primaryNupkg.Name)"

    # Step 5b: Verify pointer package is signed (smoke test)
    if ($VerifySignature) {
        Write-Step "Checking pointer package signature..."
        if (-not (Test-NupkgSignature $primaryNupkg.FullName)) {
            Write-Err "Pointer package $($primaryNupkg.Name) is NOT signed (no .signature.p7s)"
            exit 1
        }
        Write-Ok "Pointer package is signed"
    }

    # Step 6: Functional tests — install tool and exercise CLI
    if ($RunFunctionalTests) {
        Write-Host ""
        Write-Host "--- Functional Tests ---" -ForegroundColor Magenta

        # Derive the package version from the RID-specific nupkg filename
        # Filename pattern: Aspire.Cli.<rid>.<version>.nupkg
        $versionMatch = $ridNupkg.Name -replace "^Aspire\.Cli\.$([regex]::Escape($Rid))\.", '' -replace '\.nupkg$', ''
        Write-Step "Detected package version: $versionMatch"

        # Install as a local tool-path tool from the packages directory
        $toolInstallDir = Join-Path ([System.IO.Path]::GetTempPath()) "aspire-tool-install-$([System.IO.Path]::GetRandomFileName())"
        New-Item -ItemType Directory -Path $toolInstallDir -Force | Out-Null

        Write-Step "Installing Aspire.Cli tool (version $versionMatch) from $effectiveDir ..."
        $installArgs = @(
            'tool', 'install', 'Aspire.Cli',
            '--tool-path', $toolInstallDir,
            '--add-source', $effectiveDir,
            '--version', $versionMatch
        )
        $installOutput = & dotnet @installArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Err "'dotnet tool install' failed (exit code $LASTEXITCODE)"
            Write-Host ($installOutput -join "`n")
            exit 1
        }
        Write-Ok "Tool installed to $toolInstallDir"

        # Determine the shim path
        $shimName = if ($Rid -like 'win-*') { "aspire.exe" } else { "aspire" }
        $shimPath = Join-Path $toolInstallDir $shimName
        if (-not (Test-Path $shimPath)) {
            # On Windows, NativeAOT tools get a .cmd shim
            $shimPath = Join-Path $toolInstallDir "aspire.cmd"
        }
        if (-not (Test-Path $shimPath)) {
            Write-Err "Could not find aspire shim in $toolInstallDir"
            Write-Host "Contents:"
            Get-ChildItem $toolInstallDir | ForEach-Object { Write-Host "  $($_.Name)" }
            exit 1
        }

        # Step 7: aspire --version
        Write-Step "Running 'aspire --version' ..."
        $env:ASPIRE_CLI_TELEMETRY_OPTOUT = 'true'
        $env:DOTNET_CLI_TELEMETRY_OPTOUT = 'true'
        $env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = 'true'
        $env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
        $versionOutput = & $shimPath --version 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Err "'aspire --version' failed (exit code $LASTEXITCODE)"
            Write-Host ($versionOutput -join "`n")
            exit 1
        }
        Write-Ok "'aspire --version' returned: $($versionOutput -join ' ')"

        # Step 8: aspire new — exercises bundle self-extraction + template scaffolding
        $projectDir = Join-Path ([System.IO.Path]::GetTempPath()) "aspire-verify-project-$([System.IO.Path]::GetRandomFileName())"
        New-Item -ItemType Directory -Path $projectDir -Force | Out-Null

        Write-Step "Running 'aspire new aspire-starter' ..."
        $newOutput = & $shimPath new aspire-starter --name VerifyApp --output $projectDir --non-interactive --nologo 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Err "'aspire new' failed (exit code $LASTEXITCODE)"
            Write-Host ($newOutput -join "`n")
            exit 1
        }

        $appHostDir = Join-Path $projectDir "VerifyApp.AppHost"
        if (Test-Path $appHostDir) {
            Write-Ok "'aspire new' created project successfully"
        } else {
            Write-Err "'aspire new' did not create expected VerifyApp.AppHost directory"
            Get-ChildItem $projectDir -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $($_.Name)" }
            exit 1
        }

        # Cleanup project dir
        if (Test-Path $projectDir) {
            Remove-Item -Recurse -Force $projectDir -ErrorAction SilentlyContinue
        }
    }

    Write-Host ""
    Write-Host "=========================================="
    Write-Host "  All tool nupkg checks passed!" -ForegroundColor Green
    Write-Host "=========================================="
    Write-Host ""
}
catch {
    Write-Err "Verification failed: $_"
    Write-Host $_.ScriptStackTrace
    exit 1
}
finally {
    if ($extractDir -and (Test-Path $extractDir)) {
        Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
    }
    if ($toolInstallDir -and (Test-Path $toolInstallDir)) {
        Remove-Item -Recurse -Force $toolInstallDir -ErrorAction SilentlyContinue
    }
}

# Explicitly exit 0 so $LASTEXITCODE is set for callers.
# The catch block already calls exit 1 on any failure path.
exit 0
