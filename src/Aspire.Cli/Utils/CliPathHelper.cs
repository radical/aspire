// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Hosting.Backchannel;
using Microsoft.Extensions.Logging;

namespace Aspire.Cli.Utils;

internal static class CliPathHelper
{
    internal static string GetAspireHomeDirectory(string? processPath = null, ILogger? logger = null)
    {
        var effectiveProcessPath = processPath ?? Environment.ProcessPath;

        return TryGetAspireHomeDirectoryFromInstallRoute(effectiveProcessPath, logger)
            ?? Path.Combine(GetUserProfileDirectory(), ".aspire");
    }

    internal static string? TryGetAspireHomeDirectoryFromInstallRoute(string? processPath, ILogger? logger = null)
    {
        if (string.IsNullOrEmpty(processPath))
        {
            return null;
        }

        var realBinaryPath = ResolveSymlinkOrOriginalPath(processPath, logger);
        var binaryDir = Path.GetDirectoryName(realBinaryPath);
        if (string.IsNullOrEmpty(binaryDir))
        {
            return null;
        }

        var sidecarPath = Path.Combine(binaryDir, InstallSidecarReader.SidecarFileName);
        var source = InstallSidecarReader.ReadSourceField(sidecarPath);

        return source switch
        {
            InstallSourceExtensions.ScriptWire
                or InstallSourceExtensions.LocalHiveWire => Path.GetDirectoryName(binaryDir) ?? binaryDir,
            InstallSourceExtensions.PrWire => TryGetPrInstallPrefix(binaryDir),
            _ => null
        };
    }

    private static string? TryGetPrInstallPrefix(string binaryDir)
    {
        var prDir = Path.GetDirectoryName(binaryDir);
        if (string.IsNullOrEmpty(prDir))
        {
            return null;
        }

        var dogfoodDir = Path.GetDirectoryName(prDir);
        if (string.IsNullOrEmpty(dogfoodDir) ||
            !string.Equals(Path.GetFileName(dogfoodDir), InstallationDiscoveryLayout.DogfoodDirectoryName, StringComparison.Ordinal))
        {
            return null;
        }

        return Path.GetDirectoryName(dogfoodDir);
    }

    internal static string GetUserProfileDirectory()
        => Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    internal static string ResolveSymlinkOrOriginalPath(string path, ILogger? logger = null)
    {
        if (string.IsNullOrEmpty(path))
        {
            return path;
        }

        return TryResolveSymlinkTarget(path, logger, "using the raw path") ?? path;
    }

    internal static string? ResolveSymlinkToFullPath(string? path, ILogger? logger = null)
    {
        if (string.IsNullOrEmpty(path))
        {
            return null;
        }

        var resolved = TryResolveSymlinkTarget(path, logger, "trying the normalized path");
        if (resolved is not null)
        {
            return resolved;
        }

        try
        {
            return Path.GetFullPath(path);
        }
        catch (Exception ex) when (IsPathResolutionException(ex))
        {
            logger?.LogDebug(ex, "Could not normalize path {Path}; skipping it.", path);
            return null;
        }
    }

    /// <summary>
    /// Creates a randomized CLI-managed socket path.
    /// </summary>
    /// <param name="socketPrefix">The socket file prefix.</param>
    internal static string CreateUnixDomainSocketPath(string socketPrefix)
        => CreateSocketPath(socketPrefix, isGuestAppHost: false);

    internal static string CreateGuestAppHostSocketPath(string socketPrefix)
        => CreateSocketPath(socketPrefix, isGuestAppHost: true);

    private static string CreateSocketPath(string socketPrefix, bool isGuestAppHost)
    {
        var socketName = $"{socketPrefix}.{BackchannelConstants.CreateRandomIdentifier()}";

        if (isGuestAppHost && OperatingSystem.IsWindows())
        {
            return socketName;
        }

        var socketDirectory = GetCliSocketDirectory();
        Directory.CreateDirectory(socketDirectory);
        return Path.Combine(socketDirectory, socketName);
    }

    private static string GetCliHomeDirectory()
        => Path.Combine(GetAspireHomeDirectory(), "cli");

    private static string GetCliRuntimeDirectory()
        => Path.Combine(GetCliHomeDirectory(), "runtime");

    private static string GetCliSocketDirectory()
        => Path.Combine(GetCliRuntimeDirectory(), "sockets");

    private static string? TryResolveSymlinkTarget(string path, ILogger? logger, string fallbackDescription)
    {
        try
        {
            var resolved = File.ResolveLinkTarget(path, returnFinalTarget: true);
            return resolved?.FullName;
        }
        catch (Exception ex) when (IsPathResolutionException(ex))
        {
            logger?.LogDebug(ex, "Could not resolve symlink target for {Path}; {FallbackDescription}.", path, fallbackDescription);
            return null;
        }
    }

    private static bool IsPathResolutionException(Exception ex)
        => ex is IOException
            or UnauthorizedAccessException
            or ArgumentException
            or NotSupportedException
            or PathTooLongException
            or System.Security.SecurityException;
}
