// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.RegularExpressions;
using Aspire.Cli.Bundles;
using Aspire.Cli.Configuration;
using Aspire.Cli.Utils;
using Aspire.Shared;

namespace Aspire.Cli.Uninstall;

internal sealed partial class CliCleanupService(CliExecutionContext executionContext, IConfigurationService configurationService)
{
    private static readonly StringComparison s_pathComparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;

    // Hive / channel names are concatenated into HivesDirectory/<name> and
    // dogfood/<name> and then passed to Directory.Delete(recursive: true), so
    // any path separator, leading dot, or `..` segment would let the removal
    // target escape the hives directory. Mirrors localhive.{sh,ps1}'s
    // is_valid_hivename / Test-HiveName so the same names are accepted across
    // CLI and installer tooling.
    [GeneratedRegex(@"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    private static partial Regex SafeHiveNameRegex();

    internal static bool IsValidHiveName(string? name)
    {
        if (string.IsNullOrEmpty(name))
        {
            return false;
        }

        if (name.Contains("..", StringComparison.Ordinal))
        {
            return false;
        }

        return SafeHiveNameRegex().IsMatch(name);
    }

    public DirectoryInfo AspireHomeDirectory => executionContext.HivesDirectory.Parent ?? executionContext.AspireHomeDirectory;

    public IReadOnlyList<HiveInfo> GetHives()
    {
        if (!executionContext.HivesDirectory.Exists)
        {
            return [];
        }

        // Filter to names that pass IsValidHiveName so GetDogfoodDirectory
        // doesn't throw on non-standard manually-created directories like
        // ".hidden" or names with separators (the filesystem can hold them
        // even though our producers don't write them).
        return executionContext.HivesDirectory
            .EnumerateDirectories()
            .Where(d => IsValidHiveName(d.Name))
            .OrderBy(d => d.Name, StringComparer.Ordinal)
            .Select(d => new HiveInfo(d.Name, d.FullName, GetDogfoodDirectory(d.Name).Exists))
            .ToList();
    }

    public string GetHivePath(string channel)
    {
        if (!IsValidHiveName(channel))
        {
            throw new ArgumentException("Invalid hive name.", nameof(channel));
        }

        return GetHiveDirectory(channel).FullName;
    }

    public bool HasHive(string channel)
        => IsValidHiveName(channel) && GetHiveDirectory(channel).Exists;

    public IReadOnlyList<string> ExpandChannels(string? channel, bool all)
    {
        if (!all)
        {
            return string.IsNullOrWhiteSpace(channel) ? [] : [channel];
        }

        return GetHives()
            .Select(h => h.Name)
            .Where(IsIncludedInAll)
            .ToList();
    }

    public async Task<CleanupResult> DeleteHiveAsync(string channel, bool force, bool dryRun, CancellationToken cancellationToken)
    {
        var operations = new List<CleanupOperation>();
        if (!IsValidHiveName(channel))
        {
            operations.Add(CleanupOperation.Failed(channel ?? string.Empty, "Invalid hive name. Hive names must match [A-Za-z0-9][A-Za-z0-9._-]* and cannot contain path separators or '..'."));
            return new CleanupResult(operations, HasFailures: true);
        }

        var hiveDirectory = GetHiveDirectory(channel);
        var dogfoodDirectory = GetDogfoodDirectory(channel);

        if (dogfoodDirectory.Exists && !force)
        {
            operations.Add(CleanupOperation.Skipped(
                hiveDirectory.FullName,
                $"A matching dogfood install exists. Run 'aspire uninstall --channel {channel}' to remove both the hive and dogfood install, or pass --force to delete only the hive."));
            return new CleanupResult(operations, HasFailures: true);
        }

        var currentProcessPath = CliPathHelper.ResolveSymlinkToFullPath(Environment.ProcessPath);
        operations.Add(await DeleteDirectoryAsync(hiveDirectory, currentProcessPath, dryRun, cancellationToken));
        return new CleanupResult(operations, operations.Any(o => o.Status is CleanupOperationStatus.Failed));
    }

    public async Task<CleanupResult> UninstallAsync(IReadOnlyList<string> channels, bool removeSharedInstall, bool dryRun, CancellationToken cancellationToken)
    {
        var operations = new List<CleanupOperation>();
        var currentProcessPath = CliPathHelper.ResolveSymlinkToFullPath(Environment.ProcessPath);

        foreach (var channel in channels)
        {
            if (!IsValidHiveName(channel))
            {
                operations.Add(CleanupOperation.Failed(channel ?? string.Empty, "Invalid hive name. Hive names must match [A-Za-z0-9][A-Za-z0-9._-]* and cannot contain path separators or '..'."));
                continue;
            }

            var hiveDirectory = GetHiveDirectory(channel);
            operations.Add(await DeleteDirectoryAsync(hiveDirectory, currentProcessPath, dryRun, cancellationToken));

            if (IsPrChannel(channel))
            {
                var dogfoodDirectory = GetDogfoodDirectory(channel);
                if (dogfoodDirectory.Exists)
                {
                    operations.Add(DeleteDirectoryUnlessRunningFromTarget(dogfoodDirectory, currentProcessPath, dryRun));
                }
            }

            await DeleteMatchingGlobalChannelAsync(channel, dryRun, operations, cancellationToken);
        }

        if (removeSharedInstall)
        {
            AddSharedInstallOperations(currentProcessPath, dryRun, operations);
        }
        else if (channels.Any(IsSharedScriptChannel) && SharedInstallExists())
        {
            operations.Add(CleanupOperation.Skipped(
                GetSharedBinDirectory().FullName,
                "Shared script install artifacts were left in place. Pass --remove-shared-install to remove ~/.aspire/bin/aspire and the matching bundle/versions layout."));
        }

        return new CleanupResult(operations, operations.Any(o => o.Status is CleanupOperationStatus.Failed));
    }

    private async Task DeleteMatchingGlobalChannelAsync(string channel, bool dryRun, List<CleanupOperation> operations, CancellationToken cancellationToken)
    {
        var globalConfig = await configurationService.GetGlobalConfigurationAsync(cancellationToken);
        if (globalConfig.TryGetValue("channel", out var configuredChannel) &&
            string.Equals(configuredChannel, channel, StringComparison.Ordinal))
        {
            if (dryRun)
            {
                operations.Add(CleanupOperation.WouldRemove(configurationService.GetSettingsFilePath(isGlobal: true), "matching global channel"));
                return;
            }

            var deleted = await configurationService.DeleteConfigurationAsync("channel", isGlobal: true, cancellationToken);
            operations.Add(deleted
                ? CleanupOperation.Removed(configurationService.GetSettingsFilePath(isGlobal: true), "matching global channel")
                : CleanupOperation.Failed(configurationService.GetSettingsFilePath(isGlobal: true), "Matching global channel could not be deleted."));
        }
    }

    private DirectoryInfo GetHiveDirectory(string channel)
    {
        if (!IsValidHiveName(channel))
        {
            // Defense in depth: callers should validate first (and surface a
            // friendlier error), but if they don't we refuse to compose a
            // path that could escape HivesDirectory.
            throw new ArgumentException("Invalid hive name.", nameof(channel));
        }

        return new(Path.Combine(executionContext.HivesDirectory.FullName, channel));
    }

    private DirectoryInfo GetDogfoodDirectory(string channel)
    {
        if (!IsValidHiveName(channel))
        {
            throw new ArgumentException("Invalid hive name.", nameof(channel));
        }

        return new(Path.Combine(AspireHomeDirectory.FullName, "dogfood", channel));
    }

    private DirectoryInfo GetSharedBinDirectory()
        => new(Path.Combine(AspireHomeDirectory.FullName, "bin"));

    private bool SharedInstallExists()
        => EnumerateBaseSharedInstallTargets().Any(t => t.Exists);

    private IEnumerable<FileSystemInfo> EnumerateBaseSharedInstallTargets()
    {
        var binDirectory = GetSharedBinDirectory();
        var binaryPath = Path.Combine(binDirectory.FullName, OperatingSystem.IsWindows() ? "aspire.exe" : "aspire");
        yield return new FileInfo(binaryPath);
        yield return new FileInfo(Path.Combine(binDirectory.FullName, ".aspire-install.json"));
        yield return new DirectoryInfo(Path.Combine(AspireHomeDirectory.FullName, BundleDiscovery.BundleDirectoryName));
    }

    private void AddSharedInstallOperations(string? currentProcessPath, bool dryRun, List<CleanupOperation> operations)
    {
        // Resolve the bundle symlink target BEFORE deleting the base targets:
        // EnumerateBaseSharedInstallTargets yields the bundle symlink itself,
        // and once it is deleted ResolveLinkTarget can no longer recover the
        // versions/<v>/ tree it pointed at, which would silently strand that
        // tree on disk.
        var bundleDirectory = new DirectoryInfo(Path.Combine(AspireHomeDirectory.FullName, BundleDiscovery.BundleDirectoryName));
        var bundleVersionResult = ResolveBundleVersionTarget(bundleDirectory);

        foreach (var target in EnumerateBaseSharedInstallTargets())
        {
            operations.Add(DeleteFileSystemInfoUnlessRunningFromTarget(target, currentProcessPath, dryRun));
        }

        switch (bundleVersionResult)
        {
            case BundleVersionTargetResult.Target target:
                // Skip if another aspire process holds an active lease on this
                // bundle version. Matches BundleService.TryCleanupStaleVersions
                // which refuses to delete leased entries.
                if (BundleVersionLease.HasActiveLease(target.Directory.FullName))
                {
                    operations.Add(CleanupOperation.Skipped(
                        target.Directory.FullName,
                        "Another running CLI / AppHost holds an active lease on this bundle version. Stop those processes and re-run cleanup."));
                }
                else
                {
                    operations.Add(DeleteFileSystemInfoUnlessRunningFromTarget(target.Directory, currentProcessPath, dryRun));
                }
                break;
            case BundleVersionTargetResult.NotApplicable:
                break;
            case BundleVersionTargetResult.ResolveFailed failure:
                operations.Add(CleanupOperation.Failed(bundleDirectory.FullName, $"Could not resolve bundle link target to clean up versions/<v>/: {failure.Reason}"));
                break;
        }
    }

    private BundleVersionTargetResult ResolveBundleVersionTarget(DirectoryInfo bundleDirectory)
    {
        try
        {
            var resolvedTarget = bundleDirectory.ResolveLinkTarget(returnFinalTarget: true);
            if (resolvedTarget is not DirectoryInfo targetDirectory)
            {
                return new BundleVersionTargetResult.NotApplicable();
            }

            var versionsDirectory = new DirectoryInfo(Path.Combine(AspireHomeDirectory.FullName, BundleService.VersionsDirectoryName));
            if (!IsPathUnderTarget(targetDirectory.FullName, versionsDirectory.FullName))
            {
                return new BundleVersionTargetResult.NotApplicable();
            }

            return new BundleVersionTargetResult.Target(targetDirectory);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException or System.Security.SecurityException)
        {
            return new BundleVersionTargetResult.ResolveFailed(ex.Message);
        }
    }

    private abstract record BundleVersionTargetResult
    {
        internal sealed record Target(DirectoryInfo Directory) : BundleVersionTargetResult;
        internal sealed record NotApplicable : BundleVersionTargetResult;
        internal sealed record ResolveFailed(string Reason) : BundleVersionTargetResult;
    }

    private static bool IsPrChannel(string channel)
        => channel.StartsWith("pr-", StringComparison.Ordinal);

    private static bool IsSharedScriptChannel(string channel)
        => channel is "stable" or "staging" or "daily";

    private static bool IsIncludedInAll(string channel)
        => channel is "staging" or "daily" || IsPrChannel(channel);

    private static CleanupOperation DeleteDirectoryUnlessRunningFromTarget(DirectoryInfo directory, string? currentProcessPath, bool dryRun)
        => DeleteFileSystemInfoUnlessRunningFromTarget(directory, currentProcessPath, dryRun);

    internal static CleanupOperation DeleteFileSystemInfoUnlessRunningFromTarget(FileSystemInfo target, string? currentProcessPath, bool dryRun)
    {
        if (!target.Exists)
        {
            return CleanupOperation.Skipped(target.FullName, "does not exist");
        }

        if (currentProcessPath is not null && IsPathUnderTarget(currentProcessPath, target.FullName))
        {
            return CleanupOperation.Failed(target.FullName, "The running CLI is inside this target. Re-run cleanup after this process exits or delete it manually.");
        }

        if (dryRun)
        {
            return CleanupOperation.WouldRemove(target.FullName);
        }

        try
        {
            switch (target)
            {
                case DirectoryInfo directory:
                    directory.Delete(recursive: true);
                    break;
                case FileInfo file:
                    file.Delete();
                    break;
            }

            return CleanupOperation.Removed(target.FullName);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
            return CleanupOperation.Failed(target.FullName, ex.Message);
        }
    }

    private static Task<CleanupOperation> DeleteDirectoryAsync(DirectoryInfo directory, string? currentProcessPath, bool dryRun, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(DeleteFileSystemInfoUnlessRunningFromTarget(directory, currentProcessPath, dryRun));
    }

    private static bool IsPathUnderTarget(string path, string targetPath)
    {
        var normalizedPath = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        var normalizedTarget = Path.TrimEndingDirectorySeparator(Path.GetFullPath(targetPath));

        return string.Equals(normalizedPath, normalizedTarget, s_pathComparison) ||
               normalizedPath.StartsWith(normalizedTarget + Path.DirectorySeparatorChar, s_pathComparison) ||
               normalizedPath.StartsWith(normalizedTarget + Path.AltDirectorySeparatorChar, s_pathComparison);
    }
}

internal sealed record HiveInfo(string Name, string Path, bool HasMatchingDogfoodInstall);

internal sealed record CleanupResult(IReadOnlyList<CleanupOperation> Operations, bool HasFailures);

internal sealed record CleanupOperation(string Path, CleanupOperationStatus Status, string Reason)
{
    public static CleanupOperation Removed(string path, string reason = "") => new(path, CleanupOperationStatus.Removed, reason);
    public static CleanupOperation WouldRemove(string path, string reason = "") => new(path, CleanupOperationStatus.WouldRemove, reason);
    public static CleanupOperation Skipped(string path, string reason) => new(path, CleanupOperationStatus.Skipped, reason);
    public static CleanupOperation Failed(string path, string reason) => new(path, CleanupOperationStatus.Failed, reason);
}

internal enum CleanupOperationStatus
{
    Removed,
    WouldRemove,
    Skipped,
    Failed
}
