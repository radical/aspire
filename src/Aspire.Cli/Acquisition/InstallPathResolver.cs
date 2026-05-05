// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Resolves the install prefix and layout mode from a resolved binary path.
/// </summary>
internal interface IInstallPathResolver
{
    /// <summary>
    /// Determines the install <see cref="InstallMode"/> and prefix directory for the given binary path.
    /// </summary>
    /// <param name="binaryPath">
    /// The fully-resolved (symlinks collapsed) path to the running <c>aspire</c> binary.
    /// Callers are responsible for resolving shims and symlinks before invoking this method.
    /// </param>
    /// <returns>
    /// A tuple of (<see cref="InstallMode"/>, <c>prefix</c>) where <c>prefix</c> is the
    /// install root directory containing <c>.aspire-install.json</c>, or the binary directory
    /// when the mode is <see cref="InstallMode.Unknown"/>.
    /// </returns>
    (InstallMode Mode, string Prefix) Resolve(string binaryPath);
}

/// <summary>
/// Default implementation of <see cref="IInstallPathResolver"/> that applies the two-stat rule
/// (§2.4.2 of the agreed design) to discover install mode and prefix from the sidecar location.
/// </summary>
internal sealed class InstallPathResolver : IInstallPathResolver
{
    private const string SidecarFileName = ".aspire-install.json";

    /// <inheritdoc/>
    public (InstallMode Mode, string Prefix) Resolve(string binaryPath)
    {
        var binaryDir = Path.GetDirectoryName(Path.GetFullPath(binaryPath)) ?? string.Empty;

        // Step 3a: Mode B — sidecar next to the binary (binary is at the prefix root).
        if (File.Exists(Path.Combine(binaryDir, SidecarFileName)))
        {
            return (InstallMode.B, binaryDir);
        }

        // Step 3b: Mode A — sidecar one level up from the binary's directory.
        var parentDir = Path.GetDirectoryName(binaryDir);
        if (!string.IsNullOrEmpty(parentDir) && File.Exists(Path.Combine(parentDir, SidecarFileName)))
        {
            return (InstallMode.A, parentDir);
        }

        // Step 3c: Unknown — sidecar not found at either expected location.
        return (InstallMode.Unknown, binaryDir);
    }
}
