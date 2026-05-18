<#
.SYNOPSIS
    Installs the Aspire CLI from local WinGet manifest files for dogfooding.

.DESCRIPTION
    This script installs (or uninstalls) the Aspire CLI using local WinGet manifest files,
    allowing you to test builds before they are published to microsoft/winget-pkgs.

.PARAMETER ManifestPath
    Path to the directory containing the WinGet manifest YAML files.
    Defaults to auto-detecting the manifest directory relative to this script.

.PARAMETER ArchiveRoot
    Root directory containing downloaded aspire-cli-win-* archive artifacts. When present, the
    local manifest is rewritten to install from those archive files instead of ci.dot.net URLs.

.PARAMETER Uninstall
    Uninstall a previously dogfooded Aspire CLI.

.PARAMETER Force
    Allow replacing an existing Microsoft.Aspire WinGet installation.

.EXAMPLE
    .\dogfood.ps1
    # Auto-detects manifests in the script directory and installs

.EXAMPLE
    .\dogfood.ps1 -ManifestPath .\manifests\m\Microsoft\Aspire\9.2.0
    # Install from a specific manifest directory

.EXAMPLE
    .\dogfood.ps1 -ArchiveRoot ..\native-archives
    # Install using downloaded native archive artifacts

.EXAMPLE
    .\dogfood.ps1 -Uninstall
    # Uninstall the dogfooded Aspire CLI
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$ManifestPath,

    [string]$ArchiveRoot,

    [switch]$Uninstall,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-InstallerManifestPath {
    param([string]$Path)

    $installerManifests = @(Get-ChildItem -Path $Path -File -Filter "*.installer.yaml")
    if ($installerManifests.Count -ne 1) {
        Write-Error "Expected exactly one *.installer.yaml manifest under $Path, but found $($installerManifests.Count)."
        exit 1
    }

    return $installerManifests[0].FullName
}

function Get-ManifestVersion {
    param([string]$ManifestPath)

    foreach ($line in Get-Content -Path $ManifestPath) {
        if ($line -match '^\s*PackageVersion:\s*"?([^"]+)"?\s*$') {
            return $Matches[1]
        }
    }

    Write-Error "Could not read PackageVersion from $ManifestPath."
    exit 1
}

function Find-ArchiveIfPresent {
    param(
        [string]$Root,
        [string]$ArchiveName
    )

    if (-not (Test-Path $Root)) {
        return $null
    }

    return Get-ChildItem -Path $Root -File -Recurse -Filter $ArchiveName -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

function Find-Archive {
    param(
        [string]$Root,
        [string]$ArchiveName
    )

    $matches = @(Get-ChildItem -Path $Root -File -Recurse -Filter $ArchiveName -ErrorAction SilentlyContinue | Sort-Object FullName)
    if ($matches.Count -eq 0) {
        Write-Error "Could not find $ArchiveName under $Root."
        exit 1
    }

    if ($matches.Count -gt 1) {
        $matchList = $matches | ForEach-Object { "  $($_.FullName)" }
        Write-Error "Found multiple $ArchiveName archives under ${Root}:`n$($matchList -join "`n")"
        exit 1
    }

    return $matches[0].FullName
}

function Get-DefaultArchiveRoot {
    param([string]$Version)

    foreach ($candidate in @($ScriptDir, (Split-Path -Parent $ScriptDir))) {
        if ((Find-ArchiveIfPresent -Root $candidate -ArchiveName "aspire-cli-win-x64-$Version.zip") -and
            (Find-ArchiveIfPresent -Root $candidate -ArchiveName "aspire-cli-win-arm64-$Version.zip")) {
            return (Resolve-Path $candidate).Path
        }
    }

    return $null
}

function ConvertTo-FileUri {
    param([string]$Path)

    return ([System.Uri]::new((Resolve-Path $Path).ProviderPath)).AbsoluteUri
}

function Set-LocalInstallerUrls {
    param(
        [string]$InstallerManifestPath,
        [string]$ResolvedArchiveRoot,
        [string]$Version
    )

    $archiveByArchitecture = @{
        "x64"   = Find-Archive -Root $ResolvedArchiveRoot -ArchiveName "aspire-cli-win-x64-$Version.zip"
        "arm64" = Find-Archive -Root $ResolvedArchiveRoot -ArchiveName "aspire-cli-win-arm64-$Version.zip"
    }

    $currentArchitecture = $null
    $updatedLines = foreach ($line in Get-Content -Path $InstallerManifestPath) {
        if ($line -match '^\s*-\s*Architecture:\s*(\S+)\s*$') {
            $currentArchitecture = $Matches[1]
            $line
            continue
        }

        if ($line -match '^(\s*)InstallerUrl:\s*' -and $currentArchitecture -and $archiveByArchitecture.ContainsKey($currentArchitecture)) {
            "$($Matches[1])InstallerUrl: $(ConvertTo-FileUri -Path $archiveByArchitecture[$currentArchitecture])"
            continue
        }

        $line
    }

    Set-Content -Path $InstallerManifestPath -Value $updatedLines
}

if ($Uninstall) {
    Write-Host "Uninstalling dogfooded Aspire CLI..."
    Write-Host ""

    # Look for the stable Aspire package in the local installation.
    $packages = @("Microsoft.Aspire")
    foreach ($pkg in $packages) {
        Write-Host "Checking for $pkg..."
        $result = winget list --id $pkg --accept-source-agreements 2>&1
        if ($LASTEXITCODE -eq 0 -and $result -match $pkg) {
            Write-Host "  Found $pkg, uninstalling..."
            winget uninstall --id $pkg --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  Uninstalled $pkg."
            } else {
                Write-Warning "  Failed to uninstall $pkg (exit code: $LASTEXITCODE)"
            }
        }
    }

    Write-Host ""
    Write-Host "Done."
    exit 0
}

# Auto-detect manifest path if not specified
if (-not $ManifestPath) {
    if (Get-ChildItem -Path $ScriptDir -File -Filter "*.installer.yaml" | Select-Object -First 1) {
        $ManifestPath = $ScriptDir
    } else {
        # Look for versioned manifest directories under the script directory.
        # Convention: manifests/m/Microsoft/Aspire/{Version}/
        $candidates = Get-ChildItem -Path $ScriptDir -Directory -Recurse -Depth 6 |
            Where-Object {
                Test-Path (Join-Path $_.FullName "*.installer.yaml")
            } |
            Select-Object -First 1

        if ($candidates) {
            $ManifestPath = $candidates.FullName
        } else {
            Write-Error "No manifest directory found under $ScriptDir. Specify -ManifestPath explicitly."
            exit 1
        }
    }
}

if (-not (Test-Path $ManifestPath)) {
    Write-Error "Manifest path not found: $ManifestPath"
    exit 1
}

# Verify it contains manifest files
$manifestFiles = Get-ChildItem -Path $ManifestPath -Filter "*.yaml"
if ($manifestFiles.Count -eq 0) {
    Write-Error "No .yaml manifest files found in: $ManifestPath"
    exit 1
}

Write-Host "Aspire CLI WinGet Dogfood Installer"
Write-Host "====================================="
Write-Host "  Manifest path: $ManifestPath"
Write-Host "  Manifest files:"
foreach ($f in $manifestFiles) {
    Write-Host "    - $($f.Name)"
}
Write-Host ""

if (-not $Force) {
    $existingInstall = winget list --id Microsoft.Aspire --accept-source-agreements 2>&1
    if ($LASTEXITCODE -eq 0 -and $existingInstall -match "Microsoft\.Aspire") {
        Write-Error "Microsoft.Aspire is already installed. Uninstall it first, or rerun with -Force to replace it with the dogfood manifest."
        exit 1
    }
}

$installerManifestPath = Get-InstallerManifestPath -Path $ManifestPath
$version = Get-ManifestVersion -ManifestPath $installerManifestPath

if (-not $ArchiveRoot) {
    $ArchiveRoot = Get-DefaultArchiveRoot -Version $version
}

if ($ArchiveRoot) {
    $ArchiveRoot = (Resolve-Path $ArchiveRoot).Path
    Write-Host "Using local native archive artifacts from: $ArchiveRoot"
    Set-LocalInstallerUrls -InstallerManifestPath $installerManifestPath -ResolvedArchiveRoot $ArchiveRoot -Version $version
} else {
    Write-Host "No local native archive artifacts found; installing with URLs from the manifests."
}
Write-Host ""

# Enable local manifest files
Write-Host "Enabling local manifest files in winget settings..."
winget settings --enable LocalManifestFiles
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Failed to enable local manifests. You may need to run this as Administrator."
}

# Validate
Write-Host ""
Write-Host "Validating manifests..."
winget validate --manifest $ManifestPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "Manifest validation failed. Fix the manifests and try again."
    exit $LASTEXITCODE
}
Write-Host "Validation passed."

# Install
Write-Host ""
Write-Host "Installing Aspire CLI from local manifest..."
$installArgs = @("install", "--manifest", $ManifestPath, "--accept-package-agreements", "--accept-source-agreements")
if ($Force) {
    $installArgs += "--force"
}

winget @installArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Installation failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

# Refresh PATH
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

# Verify in a new process to pick up PATH changes
Write-Host ""
Write-Host "Verifying installation..."
$verifyResult = pwsh -NoProfile -Command '
    $cmd = Get-Command aspire -ErrorAction SilentlyContinue
    if (-not $cmd) { Write-Error "aspire not found in PATH"; exit 1 }
    Write-Host "  Path:    $($cmd.Source)"
    $v = & aspire --version 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Error "aspire --version failed: $v"; exit $LASTEXITCODE }
    Write-Host "  Version: $v"
' 2>&1

$verifyExitCode = $LASTEXITCODE
if ($verifyExitCode -eq 0) {
    Write-Host $verifyResult
    Write-Host ""
    Write-Host "Installed successfully!"
} else {
    Write-Host $verifyResult
    Write-Host ""
    Write-Error "Failed to verify Aspire CLI installation."
    exit $verifyExitCode
}

Write-Host ""
Write-Host "To uninstall: .\dogfood.ps1 -Uninstall"
