$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$allowedHeadRef = 'update-aspire-skills-bundle'
$protectedPaths = @(
    'src/Aspire.Cli/Agents/Hooks/track-telemetry.sh',
    'src/Aspire.Cli/Agents/Hooks/track-telemetry.ps1'
)
$bundleDirectory = 'src/Aspire.Cli/Agents/AspireSkills/Embedded'
$requiredBundlePaths = @(
    'src/Aspire.Cli/Agents/AspireSkills/AspireSkillsInstaller.cs',
    'src/Aspire.Cli/Agents/AspireSkills/Embedded/aspire-skills.metadata.json',
    'src/Aspire.Cli/Aspire.Cli.csproj'
)

# Disable rename detection so both the removed and added paths are checked.
$changedPaths = @(& git --no-pager diff --no-ext-diff --no-textconv --no-renames --name-only 'HEAD^1' HEAD -- @protectedPaths)
if ($LASTEXITCODE -ne 0)
{
    throw 'Could not compare the PR merge result with its base parent.'
}

if ($changedPaths.Count -gt 0 -and $env:PR_HEAD_REF -cne $allowedHeadRef)
{
    # A feature PR may consume a newly published bundle directly, but only when it carries
    # every generated companion change. The separate bundle-verification workflow checks
    # the archive attestation and the hook bytes against the pinned aspire-skills commit.
    $bundleChangedPaths = @(& git --no-pager diff --no-ext-diff --no-textconv --no-renames --name-only 'HEAD^1' HEAD -- @requiredBundlePaths $bundleDirectory)
    if ($LASTEXITCODE -ne 0)
    {
        throw 'Could not inspect the generated Aspire skills bundle changes.'
    }

    $missingBundlePaths = @($requiredBundlePaths | Where-Object { $_ -notin $bundleChangedPaths })
    $archiveChanged = $bundleChangedPaths | Where-Object {
        $_ -match '^src/Aspire\.Cli/Agents/AspireSkills/Embedded/aspire-skills-.*\.(zip|tar\.gz|tgz)$'
    }

    if ($missingBundlePaths.Count -gt 0 -or -not $archiveChanged)
    {
        throw "Telemetry hook scripts can only change on '$allowedHeadRef' or as part of a complete generated bundle update. Make changes in microsoft/aspire-skills and run the synchronization workflow instead of editing these copies manually."
    }

    Write-Host "Telemetry hook changes are part of a complete generated bundle update; canonical content is checked separately."
}

Write-Host 'Telemetry hook branch policy satisfied. Canonical content is checked separately.'
