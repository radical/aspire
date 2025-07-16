param(
  [Parameter(Mandatory=$true)][string] $ManifestPath,
  [Parameter(Mandatory=$false)][string] $ReleaseNotes,
  [Parameter(Mandatory=$true)][string] $GhOrganization,
  [Parameter(Mandatory=$true)][string] $GhRepository,
  [Parameter(Mandatory=$false)][string] $GhCliLink = "",
  [Parameter(Mandatory=$true)][string] $TagName,
  [bool] $DraftRelease = $false,
  [switch] $help,
  [Parameter(ValueFromRemainingArguments=$true)][String[]]$properties
)
function Write-Help() {
    Write-Host "Publish release to GitHub. Expects an environment variable GITHUB_TOKEN to perform auth."
    Write-Host "Works on Windows, macOS, and Linux. Automatically detects platform and downloads appropriate GitHub CLI."
    Write-Host "Common settings:"
    Write-Host "  -ManifestPath <value>       Path to a publishing manifest."
    Write-Host "  -ReleaseNotes <value>       Path to release notes."
    Write-Host "  -GhOrganization <value>     GitHub organization the repository lives in."
    Write-Host "  -GhRepository <value>       GitHub repository in the organization to create the release on."
    Write-Host "  -GhCliLink <value>          GitHub CLI download link (auto-detected if not specified)."
    Write-Host "  -TagName <value>            Tag to use for the release."
    Write-Host "  -DraftRelease               Stage the release, but don't make it public yet."
    Write-Host ""
}
function Get-PlatformSpecificGhCliLink()
{
    if ($GhCliLink)
    {
        return $GhCliLink
    }

    # Auto-detect platform and return appropriate GitHub CLI download link
    if ($IsWindows -or $env:OS -eq "Windows_NT")
    {
        return "https://github.com/cli/cli/releases/download/v2.75.1/gh_2.75.1_windows_amd64.zip"
    }
    elseif ($IsMacOS -or (uname -s) -eq "Darwin")
    {
        if ((uname -m) -eq "arm64")
        {
            return "https://github.com/cli/cli/releases/download/v2.75.1/gh_2.75.1_macOS_arm64.zip"
        }
        else
        {
            return "https://github.com/cli/cli/releases/download/v2.75.1/gh_2.75.1_macOS_amd64.zip"
        }
    }
    elseif ($IsLinux -or (uname -s) -eq "Linux")
    {
        return "https://github.com/cli/cli/releases/download/v2.75.1/gh_2.75.1_linux_amd64.tar.gz"
    }
    else
    {
        Write-Error "Unsupported platform. Please specify -GhCliLink manually."
        exit 1
    }
}

function Get-PlatformSpecificGhExecutable([string]$extractionPath, [string]$archiveUrl)
{
    # Extract the archive name without extension to get the parent directory
    $archiveName = [IO.Path]::GetFileNameWithoutExtension($archiveUrl)
    if ($archiveName.EndsWith(".tar"))
    {
        $archiveName = [IO.Path]::GetFileNameWithoutExtension($archiveName)
    }

    if ($IsWindows -or $env:OS -eq "Windows_NT")
    {
        return [IO.Path]::Combine($extractionPath, $archiveName, "bin", "gh.exe")
    }
    else
    {
        return [IO.Path]::Combine($extractionPath, $archiveName, "bin", "gh")
    }
}

function Get-ReleaseNotes()
{
    if ($ReleaseNotes)
    {
        if (!(Test-Path $ReleaseNotes))
        {
            Write-Error "Error: unable to find notes at $ReleaseNotes."
            exit 1
        }

        return Get-Content -Raw -Path $ReleaseNotes
    }
}

function Get-ReleasedPackages ($manifest)
{
    if ($manifest.NugetAssets.Length -eq 0)
    {
        return ""
    }

    $releasedAssetTable = "`n`n<details>`n"
    $releasedAssetTable += "<summary>Packages released to NuGet</summary>`n`n"

    foreach ($nugetPackage in $manifest.NugetAssets)
    {
        $packageName = Split-Path $nugetPackage.PublishRelativePath -Leaf
        $releasedAssetTable += "- ``" + $packageName  + "```n"
    }

    $releasedAssetTable += "</details>`n`n"

    return $releasedAssetTable
}

function Publish-GithubRelease($manifest, [string]$releaseBody)
{
    $extractionPath = New-TemporaryFile | ForEach-Object { Remove-Item $_; New-Item -ItemType Directory -Path $_ }

    $platformGhCliLink = Get-PlatformSpecificGhCliLink
    $isWindowsPlatform = $IsWindows -or $env:OS -eq "Windows_NT"
    $isMacOSPlatform = $IsMacOS -or (uname -s) -eq "Darwin"

    if ($isWindowsPlatform -or $isMacOSPlatform)
    {
        $archivePath = Join-Path $extractionPath "ghcli.zip"
    }
    else
    {
        $archivePath = Join-Path $extractionPath "ghcli.tar.gz"
    }

    $ghTool = Get-PlatformSpecificGhExecutable $extractionPath $platformGhCliLink

    Write-Host "Downloading GitHub CLI from $platformGhCliLink."
    try
    {
        $progressPreference = 'silentlyContinue'
        Invoke-WebRequest $platformGhCliLink -OutFile $archivePath

        if ($isWindowsPlatform -or $isMacOSPlatform)
        {
            Expand-Archive -Path $archivePath -DestinationPath $extractionPath
        }
        else
        {
            # Extract tar.gz on Linux
            $tarCommand = "tar -xzf `"$archivePath`" -C `"$extractionPath`" --strip-components=1"
            Invoke-Expression $tarCommand
        }

        $progressPreference = 'Continue'
    }
    catch
    {
        Write-Error "Unable to get GitHub CLI for release: $($_.Exception.Message)"
        exit 1
    }

    if (!(Test-Path $ghTool))
    {
        Write-Error "Error: unable to find GitHub tool at expected location: $ghTool"
        exit 1
    }

    # Make the gh tool executable on Unix-like systems
    if (!$isWindowsPlatform)
    {
        try
        {
            chmod +x $ghTool
        }
        catch
        {
            Write-Warning "Could not make gh tool executable. It may already be executable."
        }
    }

    if (!(Test-Path env:GITHUB_TOKEN))
    {
        Write-Error "Error: unable to find GitHub PAT. Please set in GITHUB_TOKEN."
        exit 1
    }

    $extraParameters = @()

    if ($DraftRelease -eq $true)
    {
        $extraParameters += '-d'
    }

    $releaseNotes = "release_notes.md"

    Set-Content -Path $releaseNotes -Value $releaseBody

    if (-Not (Test-Path $releaseNotes)) {
        Write-Error "Unable to find release notes"
    }

    $releaseNotes = $(Get-ChildItem $releaseNotes).FullName
    & $ghTool release create $TagName `
        --repo "`"$GhOrganization/$GhRepository`"" `
        --title "`"Aspire Release - $TagName`"" `
        --notes-file "`"$releaseNotes`"" `
        --target $manifest.Commit `
        ($extraParameters -join ' ')

    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Error "Something failed in creating the release."
        exit 1
    }
}

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ($help -or (($null -ne $properties) -and ($properties.Contains('/help') -or $properties.Contains('/?')))) {
    Write-Help
    exit 1
}

if ($null -ne $properties) {
    Write-Error "Unexpected extra parameters: $properties."
    exit 1
}

if (!(Test-Path $ManifestPath))
{
    Write-Error "Error: unable to find manifest at $ManifestPath."
    exit 1
}

$manifestSize = $(Get-ChildItem $ManifestPath).length / 1kb

# Limit size. For large manifests
if ($manifestSize -gt 500)
{
    Write-Error "Error: Manifest $ManifestPath too large."
    exit 1
}

$manifestJson = Get-Content -Raw -Path $ManifestPath | ConvertFrom-Json
$releaseNotesText = Get-ReleaseNotes
$releaseNotesText += Get-ReleasedPackages $manifestJson

Publish-GithubRelease -manifest $manifestJson `
                -releaseBody $releaseNotesText `
